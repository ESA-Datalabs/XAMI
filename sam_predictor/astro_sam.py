from sympy import N
import torch
import numpy as np
from tqdm import tqdm
from pycocotools import mask as maskUtils
import matplotlib.pyplot as plt
import torch.nn.functional as F
import cv2
from losses import loss_utils
from dataset import dataset_utils
from segment_anything.utils.transforms import ResizeLongestSide
import matplotlib.patches as patches
import numpy as np
from sam_predictor import predictor_utils
from . import preprocess
from torch.nn.functional import threshold, normalize
from yolo_predictor import yolo_predictor_utils
import torch.nn as nn

class AstroSAM:
    def __init__(self, model, device, predictor, residual_block):
        self.model = model
        self.device = device
        self.predictor = predictor
        self.transform = ResizeLongestSide(self.model.image_encoder.img_size)
        self.residual_block = residual_block    
            
    def one_image_predict(
        self,
        image_masks, 
        input_masks, 
        input_bboxes, 
        image_embedding, 
        original_image_size, 
        input_size, 
        negative_mask, 
        input_image, 
        cr_transforms=None, 
        show_plot=False):

        ious = []
        image_loss=[]
        boxes, masks, coords, coords_labels = [], [], [], []
        gt_rle_to_masks, mask_areas = [], []
        gt_numpy_bboxes = []
        for k in image_masks: 
            prompt_box = np.array(input_bboxes[k])
            gt_numpy_bboxes.append(prompt_box)
            box = self.predictor.transform.apply_boxes(prompt_box, original_image_size)
            box_torch = torch.as_tensor(box, dtype=torch.float, device=self.device)
            boxes.append(box_torch)

            # process masks
            rle_to_mask = maskUtils.decode(input_masks[k]) # RLE to array
            gt_rle_to_masks.append(torch.from_numpy(rle_to_mask).to(self.device))
            mask_input_torch = torch.as_tensor(rle_to_mask, dtype=torch.float, device=self.predictor.device).unsqueeze(0)
            mask_input_torch = F.interpolate(
                mask_input_torch.unsqueeze(0), 
                size=(256, 256), 
                mode='bilinear', 
                align_corners=False)
            masks.append(mask_input_torch.squeeze(0))
            mask_areas.append(np.sum(rle_to_mask))

            # process coords and labels
            x_min, y_min, x_max, y_max = input_bboxes[k]
            x_min, y_min, x_max, y_max = map(int, [x_min, y_min, x_max, y_max])
            point_coords = np.array([(input_bboxes[k][2]+input_bboxes[k][0])/2.0, (input_bboxes[k][3]+input_bboxes[k][1])/2.0])
            point_labels = np.array([1])
            point_coords = self.predictor.transform.apply_coords(point_coords, original_image_size)
            coords_torch = torch.as_tensor(point_coords, dtype=torch.float, device=self.predictor.device).unsqueeze(0)
            labels_torch = torch.as_tensor(point_labels, dtype=torch.int, device=self.predictor.device)
            coords.append(coords_torch)
            coords_labels.append(labels_torch)

        boxes = torch.stack(boxes, dim=0)
        masks = torch.stack(masks, dim=0)
        coords = torch.stack(coords, dim=0)
        coords_labels = torch.stack(coords_labels, dim=0)
        points = (coords, coords_labels)
        gt_rle_to_masks = torch.stack(gt_rle_to_masks, dim=0)
        
        sparse_embeddings, dense_embeddings = self.model.prompt_encoder(
        points=None,
        boxes=boxes,
        masks=None, 
        )

        del box_torch, coords_torch, labels_torch, masks
        torch.cuda.empty_cache()
        
        if torch.isnan(image_embedding).any(): # !!!!!!!!!!!!!
            print('NAN in image_embedding')
            return 0.5 # image loss for now
        
        low_res_masks, iou_predictions = self.model.mask_decoder( # iou_pred [N, 1] where N - number of masks
        image_embeddings=image_embedding,
        image_pe=self.model.prompt_encoder.get_dense_pe(),
        sparse_prompt_embeddings=sparse_embeddings,
        dense_prompt_embeddings=dense_embeddings,
        multimask_output=True, # True value works better for ambiguous prompts (single points)
        )
        
        max_low_res_masks = torch.zeros((low_res_masks.shape[0], 1, 256, 256))
        max_ious = torch.zeros((iou_predictions.shape[0], 1))
        
        # Take all low_res_mask correspnding to the index of max_iou
        for i in range(low_res_masks.shape[0]):
            max_iou_index = torch.argmax(iou_predictions[i])
            max_low_res_masks[i] = low_res_masks[i][max_iou_index].unsqueeze(0)
            max_ious[i] = iou_predictions[i][max_iou_index]
            
        low_res_masks = max_low_res_masks
        iou_predictions = max_ious
        iou_image_loss = []
        pred_masks = self.model.postprocess_masks(low_res_masks, input_size, original_image_size).to(self.device)
        # Apply Gaussian filter on logits
        kernel_size, sigma = 5, 2
        gaussian_kernel = predictor_utils.create_gaussian_kernel(kernel_size, sigma).to(self.device)
        pred_masks = F.conv2d(pred_masks, gaussian_kernel, padding=kernel_size//2)
        threshold_masks = torch.sigmoid(10 * (pred_masks - self.model.mask_threshold)) # sigmoid with steepness
        gt_threshold_masks = torch.as_tensor(gt_rle_to_masks, dtype=torch.float32) 
        numpy_gt_threshold_mask = gt_threshold_masks.contiguous().detach().cpu().numpy()
        total_mask_areas = np.array(mask_areas).sum()

        for i in range(threshold_masks.shape[0]):
            iou_per_mask = loss_utils.iou_single(threshold_masks[i][0], gt_threshold_masks[i])
            ious.append(iou_per_mask)
            iou_image_loss.append((torch.abs(iou_predictions.permute(1, 0)[0][i] - iou_per_mask)) * mask_areas[i]/total_mask_areas)

        # compute weighted dice loss (smaller weights on smaller objects)
        focal = loss_utils.focal_loss_per_mask_pair(torch.squeeze(pred_masks, dim=1), gt_threshold_masks, mask_areas)
        dice = loss_utils.dice_loss_per_mask_pair(torch.squeeze(threshold_masks, dim=1), gt_threshold_masks, mask_areas) 
        mse = F.mse_loss(threshold_masks.squeeze(1), gt_threshold_masks)
        image_loss.append(5 * mse + dice) # used in SAM paper
    
        # image_loss.append(20 * focal + dice) # used in SAM paper
        # print('image_loss w/o augm', image_loss, focal, dice)
    
        transformed_losses = []
        
        # Apply consistency regulation
        if cr_transforms is not None:
            # print('Applying CR transforms on {}'.format(k))
            for cr_transform in cr_transforms:
                
                for bbox in gt_numpy_bboxes:
                    x_min, y_min, x_max, y_max = bbox  # Adjust this line based on how your bbox data is structured
                    if x_max <= x_min or y_max <= y_min:
                        print("Invalid bbox found:", bbox)
                        
                bboxes = np.array([np.array([box[0], box[1], box[2]-box[0], box[3]-box[1]]) for box in gt_numpy_bboxes])
                        
                transformed = cr_transform(
                    image=input_image, 
                    bboxes=bboxes.reshape(-1,4), # flatten bboxes
                    masks=gt_rle_to_masks.detach().cpu().numpy(),
                    category_id= [1] * boxes.shape[0]) # I don't use labels for the moment 
        
                transformed_image = transformed['image']
                transformed_bboxes = transformed['bboxes']
                transformed_masks = transformed['masks']
                transformed_losses.append(self.one_image_predict_transform(
                    transformed_image, 
                    transformed_bboxes, 
                    transformed_masks,
                    original_image_size, 
                    input_size,
                    self.device))

        image_loss = torch.stack(image_loss)
        iou_image_loss = torch.stack(iou_image_loss)
        image_loss = torch.mean(image_loss) + torch.mean(iou_image_loss) #* loss_scaling_factor
        if len(transformed_losses)>0:
            # transformed_losses = torch.stack(transformed_losses)
            for i in range(len(transformed_losses)):
                image_loss += transformed_losses[i]
            # image_loss += torch.mean(transformed_losses)
            image_loss = image_loss/(len(transformed_losses)+1)
        # print('image_loss w augm', image_loss)
        
        # if show_plot:
        #     for i in range(threshold_masks.shape[0]):
        #         fig, axs = plt.subplots(1, 3, figsize=(40, 20))
        #         axs[0].imshow(threshold_masks.permute(1, 0, 2, 3)[0][i].detach().cpu().numpy())
        #         axs[0].set_title(f'gt iou:{ious[i].item()}, \n'+\
        #                 f'pred iou: {iou_predictions[i].item()}\n img: {iou_image_loss[i].item()}\n {image_loss.item()}', fontsize=30)
                    
        #         axs[1].imshow(gt_threshold_masks[i].detach().cpu().numpy())
        #         axs[1].set_title(f'GT masks', fontsize=40)
                
        #         axs[2].imshow(pred_masks[i][0].detach().cpu().numpy())
        #         axs[2].set_title(f'Predicted mask', fontsize=30)
            
        #         plt.show()
        #         plt.close()
        #     fig, axs = plt.subplots(1, 3, figsize=(40, 20))
        #     axs[0].imshow(input_image)
        #     axs[0].set_title(f'{k.split(".")[0]}', fontsize=40)
            
        #     axs[1].imshow(input_image) 
        #     dataset_utils.show_masks(gt_threshold_masks.detach().cpu().numpy(), axs[1], random_color=False)
        #     axs[1].set_title(f'GT masks ', fontsize=40)
            
        #     axs[2].imshow(input_image) 
        #     dataset_utils.show_masks(threshold_masks.permute(1, 0, 2, 3)[0].detach().cpu().numpy(), axs[2], random_color=False)
        #     axs[2].set_title('Pred masks', fontsize=40)
        #     plt.savefig(f'/workspace/raid/OM_DeepLearning/XMM_OM_code_git/{k.split(".")[0]}_masks.png')
        #     plt.show()
        #     plt.close()
            
        del threshold_masks
        del numpy_gt_threshold_mask 
        del low_res_masks, iou_predictions 
        del pred_masks, gt_threshold_masks
        del rle_to_mask
        torch.cuda.empty_cache()

        return image_loss

    def one_image_predict_transform(
            self,
            transformed_image, 
            transformed_bboxes, 
            transformed_masks,
            original_image_size,
            input_size,
            show_plot=True):
        
            boxes = []
            ious = []
            image_loss=[]
            mask_areas = []
            transform = ResizeLongestSide(self.model.image_encoder.img_size)
            input_image = preprocess.transform_image(self.model, transform, transformed_image, 'dummy_augm_id', self.device)
            input_image = torch.as_tensor(input_image['image'], dtype=torch.float, device=self.predictor.device) # (B, C, 1024, 1024)
            image_embedding = self.model.image_encoder(input_image)
            
            transformed_masks = np.array([transformed_masks[i] for i in range(len(transformed_masks)) if np.any(transformed_masks[i]) and np.sum(transformed_masks[i])>20])
            # for each mask, compute the bbox enclosing the mask and put it into another array
            transformed_boxes_from_masks = []
            for k in range(len(transformed_masks)):
                mask_to_box = dataset_utils.mask_to_bbox(transformed_masks[k])
                box = (mask_to_box[0], mask_to_box[1], mask_to_box[2]-mask_to_box[0], mask_to_box[3]-mask_to_box[1])
                transformed_boxes_from_masks.append(box)
                
            transformed_boxes_from_masks = np.array(transformed_boxes_from_masks)
            for mask in transformed_masks:
                mask_area = np.sum(mask)
                mask_areas.append(mask_area)
                
            if len(transformed_masks) > len(transformed_bboxes):
                    
                fig, axs = plt.subplots(1, 2, figsize=(40, 20))
                axs[0].imshow(transformed_image)
                dataset_utils.show_masks(transformed_masks, axs[0], random_color=False)
                for box in transformed_bboxes:
                    rect = patches.Rectangle(
                        (box[0], box[1]), 
                        box[2], 
                        box[3], 
                        linewidth=1, 
                        edgecolor='r', 
                        facecolor='none')
                    axs[0].add_patch(rect)
                    
                for box in transformed_boxes_from_masks:
                    rect = patches.Rectangle(
                        (box[0], box[1]), 
                        box[2], 
                        box[3], 
                        linewidth=1, 
                        edgecolor='b', 
                        facecolor='none')
                    axs[0].add_patch(rect)
                    
                axs[0].set_title(f'masks from augm ', fontsize=40)
                axs[1].imshow(transformed_image)
                plt.show()
                plt.close()
                
            for k in range(len(transformed_masks)): 
                # prompt_box = np.array(transformed_masks[k])
                # print('mask area', np.sum(transformed_masks[k]))
                prompt_box = np.array(dataset_utils.mask_to_bbox(transformed_masks[k])) # XYXY format
                # prompt_box[0]-=2.0
                # prompt_box[1]-=2.0
                # prompt_box[2]+=2.0
                # prompt_box[3]+=2.0
                box = self.predictor.transform.apply_boxes(prompt_box, original_image_size)
                box_torch = torch.as_tensor(box, dtype=torch.float, device=self.device)
                boxes.append(box_torch)
                
                # fig, ax = plt.subplots()
                # ax.imshow(transformed_masks[k])
                # rect = patches.Rectangle(
                #     (prompt_box[0], prompt_box[1]), 
                #     prompt_box[2]-prompt_box[0], 
                #     prompt_box[3]-prompt_box[1], 
                #     linewidth=1, 
                #     edgecolor='r', 
                #     facecolor='none')
                # ax.add_patch(rect)
                # plt.show()
                # plt.close()
                
            if len(boxes)>0:
                boxes = torch.stack(boxes, dim=0)
            else:
                print("After augm, image has no bbox annotations❗️")
                boxes = None
                return torch.tensor(0.0)
                
            sparse_embeddings, dense_embeddings = self.model.prompt_encoder(
            points=None,
            boxes=boxes, # must be XYXY format
            masks=None, 
            )
            
            del boxes
            torch.cuda.empty_cache()
            
            low_res_masks, iou_predictions = self.model.mask_decoder( # iou_pred [N, 1] where N - number of masks
            image_embeddings=image_embedding,
            image_pe=self.model.prompt_encoder.get_dense_pe(),
            sparse_prompt_embeddings=sparse_embeddings,
            dense_prompt_embeddings=dense_embeddings,
            multimask_output=True, # True value works better for ambiguous prompts (single points)
            )
                        
            max_low_res_masks = torch.zeros((low_res_masks.shape[0], 1, 256, 256))
            max_ious = torch.zeros((iou_predictions.shape[0], 1))
            
            # Take all low_res_mask correspnding to the index of max_iou
            for i in range(low_res_masks.shape[0]):
                max_iou_index = torch.argmax(iou_predictions[i])
                max_low_res_masks[i] = low_res_masks[i][max_iou_index].unsqueeze(0)
                max_ious[i] = iou_predictions[i][max_iou_index]
                
            low_res_masks = max_low_res_masks
            iou_predictions = max_ious
            iou_image_loss = []
            pred_masks = self.model.postprocess_masks(low_res_masks, input_size, original_image_size).to(self.device)

            # Apply Gaussian filter on logits
            kernel_size = 5
            sigma = 2
            gaussian_kernel = predictor_utils.create_gaussian_kernel(kernel_size, sigma).to(self.device)
            
            pred_masks = F.conv2d(pred_masks, gaussian_kernel, padding=kernel_size//2)
            threshold_masks = torch.sigmoid(10 * (pred_masks - self.model.mask_threshold)) # sigmoid with steepness
            gt_threshold_masks = torch.as_tensor(transformed_masks, dtype=torch.float32).to(device=self.device)
            numpy_gt_threshold_mask = gt_threshold_masks.contiguous().detach().cpu().numpy()
            total_mask_areas = np.array(mask_areas).sum()
            for i in range(threshold_masks.shape[0]):
                if len(gt_threshold_masks)>0:
                    iou_per_mask = loss_utils.iou_single(threshold_masks[i][0], gt_threshold_masks[i])
                    ious.append(iou_per_mask)
                    iou_image_loss.append((torch.abs(iou_predictions.permute(1, 0)[0][i] - iou_per_mask)) * mask_areas[i]/total_mask_areas)
                else:
                    ious.append(0.0)
                    iou_image_loss.append(0.0)
                    
            # compute weighted dice loss (smaller weights on smaller objects)
            focal = loss_utils.focal_loss_per_mask_pair(torch.squeeze(pred_masks, dim=1), gt_threshold_masks, mask_areas)
            dice = loss_utils.dice_loss_per_mask_pair(torch.squeeze(threshold_masks, dim=1), gt_threshold_masks, mask_areas) 
            mse = F.mse_loss(threshold_masks.squeeze(1), gt_threshold_masks)
            image_loss.append(5 * mse + dice) # used in SAM paper
            image_loss = torch.stack(image_loss)
            iou_image_loss = torch.stack(iou_image_loss)
            image_loss = torch.mean(image_loss) + torch.mean(iou_image_loss) #* loss_scaling_factor
            # if show_plot:
            #     for i in range(threshold_masks.shape[0]):
            #         fig, axs = plt.subplots(1, 3, figsize=(40, 20))
            #         axs[0].imshow(threshold_masks.permute(1, 0, 2, 3)[0][i].detach().cpu().numpy())
            #         axs[0].set_title(f'gt iou:{ious[i].item()}, \n'+\
            #             f'pred iou: {iou_predictions[i].item()}\n img: {iou_image_loss[i].item()}\n {image_loss.item()}', fontsize=30)
                    
            #         axs[1].imshow(gt_threshold_masks[i].detach().cpu().numpy())
            #         axs[1].set_title(f'GT masks', fontsize=40)
                    
            #         axs[2].imshow(pred_masks[i][0].detach().cpu().numpy())
            #         axs[2].set_title(f'Predicted mask', fontsize=30)
                
            #         plt.show()
            #         plt.close()
                    
            #     fig, axs = plt.subplots(1, 3, figsize=(40, 20))
            #     axs[0].imshow(transformed_image)
            #     axs[0].set_title(f'Image', fontsize=40)
                
            #     axs[1].imshow(transformed_image)
            #     dataset_utils.show_masks(gt_threshold_masks.detach().cpu().numpy(), axs[1], random_color=False)
            #     axs[1].set_title(f'GT masks ', fontsize=40)
                
            #     axs[2].imshow(transformed_image)
            #     dataset_utils.show_masks(threshold_masks.permute(1, 0, 2, 3)[0].detach().cpu().numpy(), axs[2], random_color=False)
            #     axs[2].set_title('Pred masks', fontsize=40)
                
            #     plt.show()
            #     plt.close()

            del threshold_masks
            del numpy_gt_threshold_mask 
            del low_res_masks, iou_predictions 
            del pred_masks, gt_threshold_masks
            torch.cuda.empty_cache()

            return image_loss
            
    def train_validate_step(
        self, 
        dataloader, 
        input_dir, 
        gt_masks, 
        gt_bboxes, 
        optimizer, 
        mode,
        cr_transforms = None,
        scheduler=None,
        ):
        
        assert mode in ['train', 'validate'], "Mode must be 'train' or 'validate'"
        losses = []

        for inputs in tqdm(dataloader, desc=f'{mode[0].upper()+mode[1:]} Progress', bar_format='{l_bar}{bar:10}{r_bar}{bar:-10b}'):
            batch_loss = 0.0
            batch_size = len(inputs['image']) # sometimes, at the last iteration, there are fewer images than batch size
            for i in range(batch_size):
                image_masks = [k for k in gt_masks.keys() if k.startswith(inputs['image_id'][i])]
                input_image = torch.as_tensor(inputs['image'][i], dtype=torch.float, device=self.predictor.device) # (B, C, 1024, 1024)
                image = cv2.imread(input_dir+inputs['image_id'][i])
                image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                original_image_size = image.shape[:-1]
                input_size = (1024, 1024)
                
                # IMAGE ENCODER
                image_embedding = self.model.image_encoder(input_image) # [1, 256, 64, 64]
                image_embedding = (image_embedding+self.add_residual(input_image))/2.0
                
                # negative_mask has the size of the image
                negative_mask = np.where(image>0, True, False)
                negative_mask = torch.from_numpy(negative_mask)  
                negative_mask = negative_mask.permute(2, 0, 1)
                negative_mask = negative_mask[0]
                negative_mask = negative_mask.unsqueeze(0).unsqueeze(0)
                negative_mask = negative_mask.to(self.device)
                     
                # RUN PREDICTION ON IMAGE
                if mode == 'validate':
                    with torch.no_grad():
                        if len(image_masks)>0:
                            batch_loss += (self.one_image_predict(image_masks, gt_masks, gt_bboxes, image_embedding, 
                                                            original_image_size, input_size, negative_mask, image, cr_transforms)) 
                        # else:
                        #     print(f"{inputs['image_id'][i]} has no annotations❗️")

                if mode == 'train':
                    if len(image_masks)>0:
                        batch_loss += (self.one_image_predict(image_masks, gt_masks, gt_bboxes, image_embedding, 
                                                        original_image_size, input_size, negative_mask, image, cr_transforms)) 
                    # else:
                    #     print(f"{inputs['image_id'][i]} has no annotations❗️")
                    
            if mode == 'train':
                optimizer.zero_grad()
                batch_loss.backward()
                optimizer.step()
                
                if scheduler is not None:  # this back_step should be removed
                    scheduler.step()
                    # print("Current LR:", optimizer.param_groups[0]['lr'])
                    
                del image_embedding, negative_mask, input_image, image
                torch.cuda.empty_cache()

                losses.append(batch_loss.item()/batch_size) #/loss_scaling_factor)
            else:
                # check if batch loss is float 
                self.residual_block.eval()
                if isinstance(batch_loss, float):
                    losses.append(batch_loss/batch_size)
                else:
                    losses.append(batch_loss.detach().cpu().numpy()/batch_size)
                del batch_loss, image_embedding, negative_mask, input_image, image
			
        return np.mean(losses), self.model, self.residual_block
        
    def add_residual(self, image): # [1, 3, 1024, 1024]
        
        transform_layer = nn.Sequential(
            nn.Conv2d(3, 256, kernel_size=3, stride=4, padding=1),
            nn.ReLU(),
            nn.Conv2d(256, 256, kernel_size=3, stride=4, padding=1),
            nn.ReLU()
        ).to(self.device) # output  [1, 256, 64, 64]
                
        image_embedding = transform_layer(image)
            
        # Flatten spatial dimensions
        sequence_length = image_embedding.shape[-2] * image_embedding.shape[-1]
        batch_size = image_embedding.shape[0]
        d_model = image_embedding.shape[1]

        # Reshape the tensor to [sequence_length, batch_size, d_model]
        reshaped_tensor = image_embedding.permute(2, 3, 0, 1).reshape(sequence_length, batch_size, d_model)
        output_tensor = self.residual_block(reshaped_tensor)
        residual_image_embedding = output_tensor.view(image_embedding.shape[-2], image_embedding.shape[-1], batch_size, d_model).permute(2, 3, 0, 1)
        
        return residual_image_embedding

    def run_yolo_sam_epoch(
        self, 
        yolov8_pretrained_model,
        phase, 
        batch_size, 
        image_files, 
        images_dir, 
        num_batches, 
        optimizer=None):
        assert phase in ['train', 'val'], "Phase must be 'train' or 'val'"
        
        if phase == 'train':
            self.model.train()  
        else:
            self.model.eval() 

        epoch_sam_loss = []
        epoch_yolo_loss = []
        all_preds, all_gts, all_pred_cls, all_gt_cls, all_iou_scores, all_mask_areas, pred_images = [], [], [], [], [], [], []
        for batch_idx in tqdm(range(num_batches), desc=f'{phase[0].upper()+phase[1:]} Progress', bar_format='{l_bar}{bar:10}{r_bar}{bar:-10b}'):
            start_idx = batch_idx * batch_size
            end_idx = start_idx + batch_size
            batch_files = image_files[start_idx:end_idx]

            batch_losses_sam = []
            batch_losses_yolo = []

            for image_name in batch_files:
                image_path = images_dir + image_name
                image = cv2.imread(image_path)
                image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                
                try:
                    wt_mask, wt_image = dataset_utils.isolate_background(image, decomposition='db1', level=2, sigma=1) # wavelet decomposition for faint sources
                except Exception as e:
                    print(f"Error in wavelet decomposition: {e}")
                    wt_mask, wt_image = None, None

                obj_results = yolov8_pretrained_model.predict(image_path, verbose=False, conf=0.2) 
                
                gt_masks = yolo_predictor_utils.get_masks_from_image(images_dir, image_name) 
                gt_classes = yolo_predictor_utils.get_classes_from_image(images_dir, image_name) 

                if len(obj_results[0]) == 0 or len(gt_masks) == 0:
                    # print("No object detected in the image❗️")
                    del obj_results
                    continue
                
                input_image = predictor_utils.transform_image(self.model, self.transform, image, 'dummy_image_id', self.device)['image']
                input_image = torch.as_tensor(input_image, dtype=torch.float, device=self.device) # (B, C, 1024, 1024)

                original_image_size = image.shape[:-1]
                input_size = (1024, 1024)
                
                # sets a specific mean for each image
                image_T = np.transpose(image, (2, 1, 0))
                mean_ = np.mean(image_T[image_T>0])
                std_ = np.std(image_T[image_T>0]) 
                pixel_mean = torch.as_tensor([mean_, mean_, mean_], dtype=torch.float, device=self.device)
                pixel_std = torch.as_tensor([std_, std_, std_], dtype=torch.float, device=self.device)
        
                self.model.register_buffer("pixel_mean", torch.Tensor(pixel_mean).unsqueeze(-1).unsqueeze(-1), False) # not in SAM
                self.model.register_buffer("pixel_std", torch.Tensor(pixel_std).unsqueeze(-1).unsqueeze(-1), False) # not in SAM
                
                # IMAGE ENCODER
                image_embedding = self.model.image_encoder(input_image) # [1, 256, 64, 64]

                if torch.isnan(image_embedding).any():
                    print('NAN in image_embedding')
                    # plot the image
                    fig, ax = plt.subplots()
                    ax.imshow(image)
                    plt.show()
                    plt.close()
                
                # image_embedding = (image_embedding+self.add_residual(input_image))/2.0
                mask_areas = [np.sum(gt_mask) for gt_mask in gt_masks]
                input_boxes1 = obj_results[0].boxes.xyxy
                expand_by = 0.0
                enlarged_bbox = input_boxes1.clone() 
                enlarged_bbox[:, :2] -= expand_by  
                enlarged_bbox[:, 2:] += expand_by  
                input_boxes1 = enlarged_bbox
                input_boxes = input_boxes1.cpu().numpy()
                input_boxes = self.predictor.transform.apply_boxes(input_boxes, original_image_size)
                input_boxes = torch.from_numpy(input_boxes).to(self.device)
                sam_mask, yolo_masks = [], []
                
                non_resized_masks = obj_results[0].masks.data.cpu().numpy()
                
                for i in range(len(non_resized_masks)):
                        yolo_masks.append(cv2.resize(non_resized_masks[i], image.shape[:2][::-1], interpolation=cv2.INTER_LINEAR)) 

                for (boxes,) in yolo_predictor_utils.batch_iterator(300, input_boxes): # usually, there aren't that many annotations per image
                    sparse_embeddings, dense_embeddings = self.model.prompt_encoder(
                                points=None,
                                boxes=boxes,
                                masks=None,)

                    low_res_masks, iou_predictions = self.model.mask_decoder(
                                    image_embeddings=image_embedding,
                                    image_pe=self.model.prompt_encoder.get_dense_pe(),
                                    sparse_prompt_embeddings=sparse_embeddings,
                                    dense_prompt_embeddings=dense_embeddings,
                                    multimask_output=True,
                                )
                        
                    # self.predictor.reset_image() 
                    
                    max_low_res_masks = torch.zeros((low_res_masks.shape[0], 1, 256, 256)).to(self.device)
                    max_ious = torch.zeros((iou_predictions.shape[0], 1))
                  
                    # Take all low_res_mask correspnding to the index of max_iou
                    for i in range(low_res_masks.shape[0]):
                        max_iou_index = torch.argmax(iou_predictions[i])
                        max_low_res_masks[i] = low_res_masks[i][max_iou_index].unsqueeze(0)
                        max_ious[i] = iou_predictions[i][max_iou_index]
                       
                    low_res_masks = max_low_res_masks
                    iou_predictions = max_ious
                    pred_masks = self.model.postprocess_masks(low_res_masks, (1024, 1024), image.shape[:-1]).to(self.device)
                    # Apply Gaussian filter on logits
                    kernel_size, sigma = 5, 2
                    gaussian_kernel = predictor_utils.create_gaussian_kernel(kernel_size, sigma).to(self.device)
                    pred_masks = F.conv2d(pred_masks, gaussian_kernel, padding=kernel_size//2)
                    threshold_masks = torch.sigmoid(10 * (pred_masks - self.model.mask_threshold)) # sigmoid with steepness
                    sam_mask_pre = (threshold_masks > 0.5)*1.0
                    sam_mask.append(sam_mask_pre.squeeze(1))

                    # reshape gt_masks to same shape as predicted masks
                    gt_masks_tensor = torch.stack([torch.from_numpy(mask).unsqueeze(0) for mask in gt_masks], dim=0).to(self.device)
                    yolo_masks_tensor = torch.stack([torch.from_numpy(mask).unsqueeze(0) for mask in yolo_masks], dim=0).to(self.device)
                    # segm_loss_sam, preds, gts, gt_classes_match, pred_classes_match, ious_match = loss.segm_loss_match_iou_based(
                    #     threshold_masks, 
                    #     gt_masks_tensor, 
                    #     obj_results[0].boxes.cls.detach().cpu().numpy(), 
                    #     gt_classes, 
                    #     iou_predictions,
                    #     mask_areas)
                    
                    segm_loss_yolo, preds_yolo, gts_yolo, gt_classes_match_yolo, pred_classes_match_yolo, ious_match_yolo = loss_utils.segm_loss_match_iou_based(
                        yolo_masks_tensor, 
                        gt_masks_tensor, 
                        obj_results[0].boxes.cls.detach().cpu().numpy(), 
                        gt_classes, 
                        iou_predictions,
                        mask_areas)
                    
                    segm_loss_sam, preds, gts, gt_classes_match, pred_classes_match, ious_match  = loss_utils.segm_loss_match_hungarian_compared(
                        threshold_masks,
                        yolo_masks_tensor, 
                        gt_masks_tensor, 
                        obj_results[0].boxes.cls.detach().cpu().numpy(), 
                        gt_classes, 
                        iou_predictions,
                        wt_classes=[2.0],
                        wt_mask=wt_mask,
                        mask_areas=mask_areas)
                    
                    threshold_preds = np.array([preds[i][0]>0.5*1 for i in range(len(preds))])
                    all_preds.append(threshold_preds)
                    all_gts.append(gts)
                    all_gt_cls.append(gt_classes_match)
                    all_pred_cls.append(pred_classes_match)
                    all_iou_scores.append(ious_match)
                    all_mask_areas.append(mask_areas)
                    pred_images.append(image_name)
                    
                    batch_losses_sam.append(segm_loss_sam)
                    batch_losses_yolo.append(segm_loss_yolo)
                    del sparse_embeddings, dense_embeddings, low_res_masks, max_low_res_masks, gt_masks, 
                    del yolo_masks_tensor, segm_loss_sam, segm_loss_yolo, wt_mask, wt_image, threshold_masks, pred_masks, sam_mask_pre
                    torch.cuda.empty_cache()

                    # if phase == 'val':
                    #     fig, axes = plt.subplots(1, 4, figsize=(18, 6)) 
                        
                    #     # Plot 1: GT Masks
                    #     axes[0].imshow(image)
                    #     axes[0].set_title('GT Masks')
                    #     dataset_utils.show_masks(gt_masks_tensor.squeeze(1).squeeze(1).detach().cpu().numpy(), axes[0], random_color=True)
                        
                    #     # Plot 2: YOLO Masks
                    #     axes[1].imshow(image)
                    #     axes[1].set_title('YOLOv8n predicted Masks')
                    #     dataset_utils.show_masks(yolo_masks, axes[1], random_color=True)
                        
                    #     # Plot 3: Bounding Boxes
                    #     image1 = cv2.resize(image, (1024, 1024))
                    #     for bbox in boxes:
                    #         x1, y1, x2, y2 = bbox.detach().cpu().numpy()
                    #         x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
                    #         cv2.rectangle(image1, (x1, y1), (x2, y2), (0, 255, 0), 2) 
                    #     image1_rgb = cv2.cvtColor(image1, cv2.COLOR_BGR2RGB)
                    #     axes[2].imshow(image1_rgb)
                    #     axes[2].set_title('YOLOv8n predicted Bboxes')
                        
                    #     # Plot 4: SAM Masks
                    #     sam_masks_numpy = sam_mask[0].detach().cpu().numpy()
                    #     axes[3].imshow(image)
                    #     dataset_utils.show_masks(threshold_preds, axes[3], random_color=True)
                    #     axes[3].set_title('MobileSAM predicted masks')
                    #     plt.tight_layout() 
                    #     # plt.savefig(f'./plots/combined_plots.png')
                    #     plt.show()

                    del obj_results, pixel_mean, pixel_std, image_T, image, sam_mask, yolo_masks, input_boxes, input_boxes1
                    del threshold_preds, preds, gts, gt_classes_match, pred_classes_match, ious_match
                    torch.cuda.empty_cache()
                    
            mean_loss_sam = torch.mean(torch.stack(batch_losses_sam))
            mean_loss_yolo = torch.mean(torch.stack(batch_losses_yolo))
            epoch_sam_loss.append(mean_loss_sam.item())
            epoch_yolo_loss.append(mean_loss_yolo.item())
            
            if phase == 'train':
                optimizer.zero_grad()
                mean_loss_sam.backward()
                optimizer.step()

        # print(f'Epoch {epoch}, {phase.capitalize()} Segmentation loss SAM: {np.mean(epoch_sam_loss)}. YOLO: {np.mean(epoch_yolo_loss)}')
        
        return np.mean(epoch_sam_loss), np.mean(epoch_yolo_loss), all_preds, all_gts, all_gt_cls, all_pred_cls, all_iou_scores, all_mask_areas, pred_images
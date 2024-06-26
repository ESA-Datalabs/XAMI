import torch
from scipy.optimize import linear_sum_assignment
import torch.nn.functional as F
import numpy as np
import matplotlib.pyplot as plt
from ..model_predictor import predictor_utils

def iou_single(pred_mask, gt_mask):
    """Compute IoU between a single predicted mask and a single ground truth mask."""
    intersection = torch.sum(pred_mask * gt_mask)
    union = torch.sum(pred_mask) + torch.sum(gt_mask) - intersection
    if union == 0:
        return torch.tensor(0.)
    else:
        return intersection / union

def compute_iou_matrix(pred_masks, gt_masks):
    """
    Compute a matrix of IoU scores for each pair of predicted and GT masks.
    
    Parameters:
    - pred_masks: Tensor of shape [num_pred, H, W]
    - gt_masks: Tensor of shape [num_gt, H, W]
    
    Returns:
    - iou_matrix: Tensor of shape [num_pred, num_gt]
    """
    num_pred = pred_masks.shape[0]
    num_gt = gt_masks.shape[0]
    iou_matrix = torch.zeros((num_pred, num_gt))
    
    for i in range(num_pred):
        for j in range(num_gt):
            iou_matrix[i, j] = iou_single(pred_masks[i], gt_masks[j])
    
    return iou_matrix

# inspired from here: https://www.kaggle.com/code/aakashnain/diving-deep-into-focal-loss
def compute_focal_loss(y_pred, y_true, alpha=0.7, gamma=2.0):
    """
    Compute the focal loss between `y_true` and `y_pred`.
    
    Args:
    - y_true (torch.Tensor): Ground truth labels, shape [H, W]
    - y_pred (torch.Tensor): Predicted logits, shape [H, W]
    - alpha (float): Weighting factor.
    - gamma (float): Focusing parameter.

    Returns:
    - torch.Tensor: Computed focal loss.
    """
    p = torch.sigmoid(y_pred)
    bce = F.binary_cross_entropy_with_logits(y_pred, y_true, reduction='none')
    p_t = y_true * p + (1 - y_true) * (1 - p)
    alpha_factor = y_true * alpha + (1 - y_true) * (1 - alpha)
    modulating_factor = torch.pow((1 - p_t), gamma)
    focal_loss = alpha_factor * modulating_factor * bce

    return torch.mean(focal_loss)

def compute_dice_loss(pred_mask, gt_mask):
    """
    Compute the Dice loss between a single predicted mask and a single ground truth mask.
    Both masks should be floating-point tensors with the same shape.
    """
    pred_flat = torch.flatten(pred_mask)
    gt_flat = torch.flatten(gt_mask) 
    intersection = (pred_flat * gt_flat).sum()
    union = pred_flat.sum() + gt_flat.sum()
    dice_coefficient = (2. * intersection + 1e-6) / (union + 1e-6)  # Adding a small epsilon to avoid division by zero

    return 1 - dice_coefficient

def segm_loss_match_hungarian(
    use_yolo_masks,
	pred_masks,
	gt_masks, 
	all_pred_classes, 
	all_gt_classes, 
	iou_scores,
    mask_areas=None,
    image=None,
    yolo_masks=None,
    wt_classes=None,
    wt_threshold=None):

    # Compute IoU matrix for all pairs
    iou_matrix = compute_iou_matrix(pred_masks, gt_masks)  
    preds = []
    gts = []
    gt_classes, pred_classes, iou_scores_sam, combined_preds = [], [], [], []
    # Hungarian matching
    cost_matrix = -iou_matrix  # Negate IoU for minimization
    row_ind, col_ind = linear_sum_assignment(cost_matrix.detach().numpy())

    # Compute loss for matched pairs
    total_dice_loss = 0
    total_focal_loss = 0
    
    for pred_idx, gt_idx in zip(row_ind, col_ind):
        dice_loss = compute_dice_loss(pred_masks[pred_idx], gt_masks[gt_idx])
        focal_loss = compute_focal_loss(pred_masks[pred_idx].float(), gt_masks[gt_idx].float())
        preds.append(pred_masks[pred_idx].detach().cpu().numpy())
        gts.append(gt_masks[gt_idx].detach().cpu().numpy())
        pred_classes.append(int(all_pred_classes[pred_idx]))
        gt_classes.append(all_gt_classes[gt_idx])
        iou_scores_sam.append(iou_scores[pred_idx].detach().cpu().numpy())
        
        # if mask_areas is not None:
        #     dice_loss *= mask_areas[gt_idx]/sum(mask_areas) # weighted loss given mask size
        #     focal_loss *= mask_areas[gt_idx]/sum(mask_areas) # weighted loss given mask size
            
        if use_yolo_masks:
            if yolo_masks is not None and wt_threshold is not None and wt_classes is not None and image is not None:
                combined_preds.append(predictor_utils.process_faint_masks(
                    image, 
                    [pred_masks[pred_idx]], 
                    [yolo_masks[pred_idx]], 
                    [all_pred_classes[pred_idx]], 
                    pred_masks.device,
                    wt_threshold,
                    wt_classes
                    )[0].detach().cpu().numpy())
            
        total_dice_loss += dice_loss
        total_focal_loss += focal_loss
            
    # Normalize the losses
    mean_dice_loss = total_dice_loss / len(row_ind)
    mean_focal_loss = total_focal_loss / len(row_ind)
    # Combine losses
    total_loss = mean_dice_loss + 20 * mean_focal_loss

    if use_yolo_masks:
        preds = combined_preds
                        
    return total_loss, preds, gts, gt_classes, pred_classes, iou_scores_sam

def segm_loss_match_iou_based(
    use_yolo_masks,
	pred_masks, 
	gt_masks, 
	all_pred_classes, 
	all_gt_classes, 
	model_iou_scores,
    mask_areas,
    image=None,
    yolo_masks=None,
    wt_classes=None,
    wt_threshold=None):
    
    # Compute IoU matrix for all pairs
    iou_matrix = compute_iou_matrix(pred_masks, gt_masks)  
    preds = []
    gts = []
    new_mask_areas = []
    # Compute loss for matched pairs
    total_dice_loss = 0
    total_focal_loss = 0
    gt_classes, pred_classes, iou_scores_sam, combined_preds = [], [], [], []
    for pred_idx in range(iou_matrix.shape[0]):
        # Find the ground truth mask with the highest IoU for each predicted mask
        iou_scores = iou_matrix[pred_idx]
        gt_idx = torch.argmax(iou_scores).item()     
        preds.append(pred_masks[pred_idx].detach().cpu().numpy())
        new_mask_areas.append(mask_areas[gt_idx])
        gts.append(gt_masks[gt_idx].detach().cpu().numpy())
        pred_classes.append(int(all_pred_classes[pred_idx]))
        gt_classes.append(all_gt_classes[gt_idx])
        iou_scores_sam.append(model_iou_scores[pred_idx].detach().cpu().numpy())
        dice_loss = compute_dice_loss(pred_masks[pred_idx], gt_masks[gt_idx])
        focal_loss = compute_focal_loss(pred_masks[pred_idx].float(), gt_masks[gt_idx].float())
        if mask_areas is not None:
            total_dice_loss += (dice_loss * mask_areas[gt_idx]/sum(mask_areas)) # weighted loss given mask size
            total_focal_loss += (focal_loss * mask_areas[gt_idx]/sum(mask_areas)) # weighted loss given mask size
        if use_yolo_masks:
            if yolo_masks is not None and wt_threshold is not None and wt_classes is not None and image is not None:
                combined_preds.append(predictor_utils.process_faint_masks(
                    image, 
                    [pred_masks[pred_idx]], 
                    [yolo_masks[pred_idx]], 
                    [all_pred_classes[pred_idx]], 
                    pred_masks.device,
                    wt_threshold,
                    wt_classes
                    )[0].detach().cpu().numpy())
            
    # Normalize the losses
    mean_dice_loss = total_dice_loss / len(gts)
    mean_focal_loss = total_focal_loss / len(gts)

    # Combine losses
    total_loss = mean_dice_loss + 20 * mean_focal_loss
    
    if use_yolo_masks:
        preds = combined_preds

    return total_loss, preds, gts, gt_classes, pred_classes, iou_scores_sam, new_mask_areas

def dice_loss_per_mask_pair(pred, target, mask_areas, negative_mask=None):
    
    assert pred.size() == target.size(), "Prediction and target must have the same shape"
    batch_size, height, width = pred.size()
    total_masks_area = np.array(mask_areas).sum()
    dice_loss = 0.0
    
    for i in range(batch_size):
        pred_mask = pred[i].contiguous()
        target_mask = target[i].contiguous()
        dice_loss += (compute_dice_loss(pred_mask, target_mask) * mask_areas[i]/total_masks_area) # weighted loss given mask size
        
    return dice_loss/batch_size

def focal_loss_per_mask_pair(inputs, targets, mask_areas):
    
    assert inputs.size() == targets.size(), "Inputs and targets must have the same shape"
    batch_size, height, width = inputs.size()
    total_masks_area = np.array(mask_areas).sum()
    focal_loss = 0.0
    
    if total_masks_area == 0:
        print("Total mask area is zero")
        return focal_loss
    if batch_size == 0:
        print("batch_size is zero")
        return focal_loss  
      
    for i in range(batch_size):
        input_mask = inputs[i].unsqueeze(0)
        target_mask = targets[i].unsqueeze(0)
        focal_loss += (compute_focal_loss(input_mask, target_mask) * mask_areas[i] / total_masks_area) # weighted loss given mask size
        
    focal_loss /= batch_size
    
    return focal_loss

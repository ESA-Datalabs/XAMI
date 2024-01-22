import matplotlib.pyplot as plt
from astropy.convolution import convolve
from astropy.visualization import AsinhStretch, LogStretch, ZScaleInterval
from astropy.visualization.mpl_normalize import ImageNormalize
from photutils.background import Background2D, SExtractorBackground, MedianBackground, MMMBackground, ModeEstimatorBackground
from photutils.segmentation import (deblend_sources, detect_sources, make_2dgaussian_kernel)
from astropy.stats import biweight_location, mad_std, sigma_clipped_stats, SigmaClip
from photutils.segmentation import detect_threshold, detect_sources
from photutils.utils import circular_footprint
import numpy as np
from photutils.segmentation import SourceFinder, SourceCatalog
import os
import cv2
from astropy.io import fits
med = r"$\tilde{x}$"
biw_loc = r"$\zeta_{\text{biloc}}$"

# usually the pip install "opencv-python-headless<4.3" solves this problem:
# AttributeError: partially initialized module 'cv2' has no attribute '_registerMatType' (most likely due to a circular import)
# !pip install "opencv-python-headless<4.3"
# !pip install jupyter-bbox-widget

def data_norm(data, n_samples=1000, contrast=0.1, max_reject=0.5, min_npixels=10, krej=2.5, max_iterations=5):
    interval = ZScaleInterval(n_samples, contrast, max_reject, min_npixels, krej, max_iterations)
    vmin, vmax = interval.get_limits(data)
    norm = ImageNormalize(vmin=vmin, vmax=vmax)
    return norm

def image_stretch(data, stretch='log', factor=1):
	"""
	Apply a stretch to an image data array while masking negative input values.

	Parameters:
	- data: numpy.ndarray
		The input image data array. The image is expected to be normalised.
	- stretch: str, optional (default: 'log')
		The type of stretch to apply. Options are 'log' and 'asinh'.
	- factor: float, optional (default: 1)
		The stretching factor to apply. 

	Returns:
	- numpy.ndarray
		The stretched image data array.
	"""

	positive_mask = data > 0
	data_log_stretched = data.copy()

	if stretch == 'asinh':
		stretch = AsinhStretch(a=factor)
	elif stretch == 'log':
		stretch = LogStretch(a=factor)
	else:
		raise ValueError('Invalid stretch option')
	
	data_log_stretched[positive_mask] = stretch(data[positive_mask])
	# data_log_stretched = stretch(data)

	return data_log_stretched

def zscale_image(input_path, output_folder, with_image_stretch=False):
	hdul = fits.open(input_path)
	image_data = hdul[0].data
	hdul.close()
	
	# Apply zscale normalization only on non-negative data
	norm = data_norm(image_data[image_data>0])
	normalized_data = norm(image_data)
	normalized_data = normalized_data.filled(fill_value=-1)
	
	os.makedirs(output_folder, exist_ok=True)
	output_path = os.path.join(output_folder, os.path.basename(input_path).replace('.fits', '.png'))
	
	flipped_data = np.flipud(normalized_data)
	
	# clip to 255
	scaled_data = np.clip((flipped_data * 255), 0, 255).astype(np.uint8)
	
	if with_image_stretch:
		data_log_stretched = image_stretch(flipped_data, stretch='log', factor=500.0)
		cv2.imwrite(output_path, np.clip((data_log_stretched * 255), 0, 255).astype(np.uint8))
		print(f"FITS file saved as PNG with zscale normalization and Log stretch: {output_path}")
	else:
		cv2.imwrite(output_path, scaled_data)
		print(f"FITS file saved as PNG with zscale normalization: {output_path}") 

	return output_path

def detect_and_deblend_sources(data_orig, hw_threshold=0, clip_sigma=3.0, kernel_sigma=3.0, npixels=10, verbose=True):
	"""
	Deblends the input data using background subtraction, convolution, and source detection.

	Args:
		data (numpy.ndarray): Input data array.

	Returns:
		segment_map (numpy.ndarray): Segmentation map of the deblended segments.
		segment_map_finder (numpy.ndarray): Segmentation map of the sources on the segmented image.
		relevant_sources_tbl (astropy.table.Table): Table containing relevant sources (also based on hw threshold) information.
	"""
    
	try: 
		# Initialize SigmaClip with desired parameters
		sigma_clip = SigmaClip(sigma=clip_sigma, 
						#  sigma_lower=clip_sigma, 
						#  sigma_upper= clip_sigma+0.5, 
						 maxiters=10)
		
		# this is used to mask the regions with no data
		coverage_mask = (data_orig == 0)

		threshold = np.percentile(data_orig, 90)  
		bright_source_mask = data_orig > threshold

		# doc here: https://photutils.readthedocs.io/en/stable/api/photutils.background.SExtractorBackground.html
		bkg_estimator = ModeEstimatorBackground()
		bkg = Background2D(data_orig, 
					 box_size=(10, 10), 
					 filter_size=(5,5), 
					 sigma_clip=sigma_clip, 
					#  mask=bright_source_mask, 
					 coverage_mask = coverage_mask,
					 bkg_estimator=bkg_estimator)
		data_orig = data_orig * (~coverage_mask) # mask the regions with no data
		data = data_orig - bkg.background  # subtract the background
		# data = data * (~coverage_mask) # mask the regions with no data
		if verbose:
			print(f"Background: {bkg.background_median}\nBackground RMS: {bkg.background_rms_median}")

		threshold = 1.2 * bkg.background_rms # n-sigma threshold
		kernel = make_2dgaussian_kernel(kernel_sigma, size=5) # enhance the visibility of significant features while reducing the impact 
															  # of random noise or small irrelevant details
		convolved_data = convolve(data, kernel)
		convolved_data = convolved_data * (~coverage_mask) # mask the regions with no data
		# npixels = 10  # minimum number of connected pixels, each greater than threshold, that an object must have to be detected
		segment_map = detect_sources(convolved_data, threshold, npixels=npixels)
		segm_deblend = deblend_sources(convolved_data, segment_map, npixels=npixels, progress_bar=False)

		# Calculate source density
		num_sources = len(np.unique(segment_map)) - 1  # Subtract 1 for the background
		image_area = data.shape[0] * data.shape[1] 
		source_density = num_sources / image_area

		if verbose:
			print(f"Number of sources: {num_sources}\nImage area: {image_area}\nSource density: {source_density}")
			fig, (ax1, ax2, ax3, ax4) = plt.subplots(1, 4, figsize=(10, 4))
			ax1.imshow(data_orig, cmap='gray', origin='lower', norm=data_norm(data_orig))
			ax1.set_title('Original Data')

			ax2.imshow(data, cmap='gray', origin='lower', norm=data_norm(data))
			ax2.set_title('Background-subtracted Data')
			cmap1 = segment_map.cmap
			ax3.imshow(segment_map.data, cmap=cmap1, interpolation='nearest')
			ax3.set_title('Original Segment')
			cmap2 = segm_deblend.cmap
			ax4.imshow(segm_deblend.data, cmap=cmap2, interpolation='nearest')
			ax4.set_title('Deblended Segments')
			plt.tight_layout()
			plt.savefig('./plots/segmentation_1.png')

		finder = SourceFinder(npixels=10, progress_bar=False)
		segment_map_finder = finder(convolved_data, threshold)
		
		cat = SourceCatalog(data, segm_deblend, convolved_data=convolved_data)
		tbl = cat.to_table()

		tbl['xcentroid'].info.format = '.2f'
		tbl['ycentroid'].info.format = '.2f'
		tbl['kron_flux'].info.format = '.2f'
		tbl['kron_fluxerr'].info.format = '.2f'
		tbl['area'].info.format = '.2f'
		# print(tbl['bbox_xmax'].value - tbl['bbox_xmin'].value)
		# print(tbl['bbox_ymax'].value - tbl['bbox_ymin'].value)
		relevant_sources_tbl = tbl[(abs(tbl['bbox_xmax'].value - tbl['bbox_xmin'].value) > hw_threshold) & 
							(abs(tbl['bbox_ymax'].value - tbl['bbox_ymin'].value) > hw_threshold)]
		# print('flux:', relevant_sources_tbl['kron_flux'].value)

		if verbose:
			fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 12.5))
			ax1.imshow(data, cmap='Greys_r')
			ax1.set_title('Sources')
			ax2.imshow(segment_map_finder, cmap=segm_deblend.cmap, interpolation='nearest')
			ax2.set_title('Deblended Sources')
			cat.plot_kron_apertures(ax=ax1, color='white', lw=1.5)
			cat.plot_kron_apertures(ax=ax2, color='white', lw=1.5)
			plt.tight_layout()
			plt.show()
			plt.close()
	except Exception as e:
		print(e)
		return None, None, None	
	return segment_map, segment_map_finder, relevant_sources_tbl

def mask_with_sigma_clipping(data_2D, sigma=3.0, maxiters=10, sigma_threshold=4.5, footprint_radius=10):
	'''
	prints statistics of the image data, excluding the masked regions.
	'''
	sigma_clip = SigmaClip(sigma=sigma, maxiters=10)
	
	threshold = detect_threshold(data_2D, nsigma=sigma_threshold, sigma_clip=sigma_clip)
	segment_img = detect_sources(data_2D, threshold, npixels=10)
	footprint = circular_footprint(radius=footprint_radius)
	
	# masking can be used to isolate or ignore these sources.
	mask = segment_img.make_source_mask(footprint=footprint)
	# calculates mean, median, and standard deviation of data array, excluding the masked source regions.
	mean, median, std = sigma_clipped_stats(data_2D, sigma=3.0, mask=mask)
	print((mean, median, std))

	plt.subplot(1, 2, 1)
	plt.imshow(data_2D) 
	med = r"$\tilde{x}$"
	biw_loc = r"$\zeta_{\text{biloc}}$"
	plt.title(f'{med}= {np.median(data_2D).round(3)}, {biw_loc} = {biweight_location(data_2D).round(3)}\nmad  = {mad_std(data_2D).round(3)}')

	plt.subplot(1, 2, 2) 
	plt.imshow(mask) 
	plt.title(f'Masked region\n')

	plt.show()
	plt.close()
	return mask, mean, median, std

def get_normalized_centers(data_2D, hw_threshold=30, clip_sigma=2.5, kernel_sigma=1.5, npixels=10):
	segment_map, segment_map_finder, sources_tbl_with_hw_threshold = detect_and_deblend_sources(data_2D, hw_threshold=hw_threshold, clip_sigma=clip_sigma, 
																		  kernel_sigma=kernel_sigma, npixels=npixels, verbose=False)
    
	if segment_map and segment_map_finder and sources_tbl_with_hw_threshold:
		centers_x = [source['xcentroid'] for source in sources_tbl_with_hw_threshold]
		centers_y = [source['ycentroid'] for source in sources_tbl_with_hw_threshold]
		
		def normalize_centers(centers_x, centers_y, image_width, image_height):
				normalized_centers = []
				for x, y in zip(centers_x, centers_y):
						x_normalized = x / (image_width - 1)
						y_normalized = y / (image_height - 1)
						normalized_centers.append((x_normalized, y_normalized))
				return normalized_centers
    		
		normalized_centers = normalize_centers(centers_x, centers_y, data_2D.shape[0], data_2D.shape[1])
		return normalized_centers
	else:
		return None
		
def clahe_algo_image(IMAGE_PATH, clipLimit=3.0, tileGridSize=(8,8)):

    image = cv2.imread(IMAGE_PATH.replace(".fits", ".png"))
    image_bw = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    positive_mask = image_bw > 0
    final_img = image_bw.copy()
    
    clahe = cv2.createCLAHE(clipLimit=clipLimit, tileGridSize=tileGridSize)
    clahe_img = clahe.apply(image_bw[positive_mask])
    final_img[positive_mask] = clahe_img.flatten()
    _, ordinary_img = cv2.threshold(image_bw, 155, 255, cv2.THRESH_BINARY)
    
    plt.figure(figsize=(30, 10))
    plt.subplot(1, 4, 1)
    plt.imshow(image_bw, cmap='viridis')
    plt.title(f'Original Image {IMAGE_PATH.split("/")[-1]}')
    
    plt.subplot(1, 4, 2)
    plt.imshow(ordinary_img, cmap='viridis')
    plt.title('Ordinary Threshold')
    
    plt.subplot(1, 4, 3)
    plt.imshow(final_img, cmap='viridis')
    plt.title('CLAHE Image')
    
    plt.subplot(1, 4, 4)
    plt.hist(final_img[final_img>0], bins=100)
    plt.title('CLAHE Image histogram (nonnegative pixels)')
    plt.show()
    plt.close()

    return final_img
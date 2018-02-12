""" NIMPA: functions for neuro image processing and analysis
    including partial volume correction (PVC) and ROI extraction and analysis.
"""
__author__    = "Pawel Markiewicz"
__copyright__ = "Copyright 2018"
#-------------------------------------------------------------------------------

import numpy as np
import sys
import os
import scipy.ndimage as ndi
import nibabel as nib

from collections import namedtuple
from subprocess import call
import datetime
import re

from pkg_resources import resource_filename
import imio
import improc
import nipet

# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
# FUNCTIONS: T R I M   &   P A R T I A L   V O L U M E   E F F E C T S   A N D   C O R R E C T I O N
# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>

def trimim( fims, 
            affine=None,
            scale=2,
            divdim = 8**2,
            int_order=0,
            fmax = 0.05,
            outpath='',
            fcomment='',
            store_avg=False,
            store_img_intrmd=False,
            store_img=False,
            imdtype=np.float32,
            memlim=False,
            verbose=False):
    '''
    Trim and upsample PET image(s), e.g., for GPU execution,
    PVC correction, ROI sampling, etc.
    The input images 'fims' can be passed in multiple ways:
    1. as a string of the folder containing NIfTI files
    2. as a string of a NIfTI file path (this way a 4D image can be loaded).
    3. as a list of NIfTI file paths.
    4. as a 3D or 4D image
    Parameters:
    -----------
    affine: affine matrix, 4x4, for all the input images in case when images
            passed as a matrix (all have to have the same shape and data type)
    scale: the scaling factor for upsampling has to be greater than 1
    divdim: image divisor, i.e., the dimensions are a multiple of this number (default 64)
    int_order: interpolation order (0-nearest neighbour, 1-linear, as in scipy)
    fmax: fraction of the max image value used for finding image borders for trimming
    outpath: output folder path
    fcomment: part of the name of the output file, left for the user as a comment
    store_img_intrmd: stores intermediate images with suffix '_i'
    store_avg: stores the average image (if multiple images are given)
    imdtype: data type for output images
    memlim: Ture for cases when memory is limited and takes more processing time instead.
    verbose: verbose mode [True/False]
    '''

    # case when input folder is given
    if isinstance(fims, basestring) and os.path.isdir(fims):
        # list of input images (e.g., PET)
        fimlist = [os.path.join(fims,f) for f in os.listdir(fims) if f.endswith('.nii') or f.endswith('.nii.gz')]
        imdic = imio.niisort(fimlist)
        imin = imdic['im']
        imshape = imdic['shape']
        affine = imdic['affine']
        fldrin = fims
        fnms = [os.path.basename(f).split('.')[0] for f in imdic['files'] if f!=None]

    # case when input file is a 3D or 4D NIfTI image
    elif isinstance(fims, basestring) and os.path.isfile(fims) and (fims.endswith('nii') or fims.endswith('nii.gz')):
        imdic = imio.getnii(fims, output='all')
        imin = imdic['im']
        if imin.ndim==3:
            imin.shape = (1, imin.shape[0], imin.shape[1], imin.shape[2])
        imdtype = imdic['dtype']
        imshape = imdic['shape'][-3:]
        affine = imdic['affine']
        fldrin = os.path.dirname(fims)
        fnms = imin.shape[0] * [ os.path.basename(fims).split('.')[0] ]

    # case when a list of input files is given
    elif isinstance(fims, list) and all([os.path.isfile(k) for k in fims]):
        imdic = imio.niisort(fims)
        imin = imdic['im']
        imshape = imdic['shape']
        affine = imdic['affine']
        fldrin = os.path.dirname(fims[0])
        fnms = [os.path.basename(f).split('.')[0] for f in imdic['files']]

    # case when an array [#frames, zdim, ydim, xdim].  Can be 3D or 4D
    elif isinstance(fims, (np.ndarray, np.generic)) and (fims.ndim==4 or fims.ndim==3):
        # check image affine
        if affine.shape!=(4,4):
            raise ValueError('Affine should be a 4x4 array.')
        # create a copy to avoid mutation when only one image (3D)
        imin = np.copy(fims)
        if fims.ndim==3:
            imin.shape = (1, imin.shape[0], imin.shape[1], imin.shape[2])
        imshape = imin.shape[-3:]
        fldrin = os.path.join(os.path.expanduser('~'), 'NIMPA_output')
        fnms = imin.shape[0] * [ 'NIMPA' ]

    else:
        raise TypeError('Wrong data type input.')

    # number of images/frames
    Nim = imin.shape[0]

    #-------------------------------------------------------
    # store images in this folder
    if outpath=='':
        petudir = os.path.join(fldrin, 'trimmed')
    else:
        petudir = os.path.join(outpath, 'trimmed')
    imio.create_dir( petudir )
    #-------------------------------------------------------

    #-------------------------------------------------------
    # scale is preferred to be integer
    try:
        scale = int(scale)
    except ValueError:
        raise ValueError('e> scale has to be an integer.')
    # scale factor as the inverse of scale
    sf = 1/float(scale)
    if verbose:
        print 'i> upsampling scale {}, giving resolution scale factor {} for {} images.'.format(scale, sf, Nim)
    #-------------------------------------------------------

    #-------------------------------------------------------
    # scaled input image and get a sum image as the base for trimming
    if scale>1:
        newshape = (scale*imshape[0], scale*imshape[1], scale*imshape[2])
        imsum = np.zeros(newshape, dtype=imdtype)
        if not memlim:
            imscl = np.zeros((Nim,)+newshape, dtype=imdtype)
            for i in range(Nim):
                imscl[i,:,:,:] = ndi.interpolation.zoom(imin[i,:,:,:], (scale, scale, scale), order=int_order )
                imsum += imscl[i,:,:,:]
        else:
            for i in range(Nim):
                imsum += ndi.interpolation.zoom(imin[i,:,:,:], (scale, scale, scale), order=0 )
    else:
        imscl = imin
        imsum = np.sum(imin, axis=0)

    # smooth the sum image for improving trimming (if any)
    #imsum = ndi.filters.gaussian_filter(imsum, imio.fwhm2sig(4.0, voxsize=abs(affine[0,0])), mode='mirror')
    #-------------------------------------------------------
   
    # find the object bounding indexes in x, y and z axes, e.g., ix0-ix1 for the x axis
    qx = np.sum(imsum, axis=(0,1))
    ix0 = np.argmax( qx>(fmax*np.nanmax(qx)) )
    ix1 = ix0+np.argmin( qx[ix0:]>(fmax*np.nanmax(qx)) )

    qy = np.sum(imsum, axis=(0,2))
    iy0 = np.argmax( qy>(fmax*np.nanmax(qy)) )
    iy1 = iy0+np.argmin( qy[iy0:]>(fmax*np.nanmax(qy)) )

    qz = np.sum(imsum, axis=(1,2))
    iz0 = np.argmax( qz>(fmax*np.nanmax(qz)) )

    # find the maximum voxel range for x and y axes
    IX = ix1-ix0+1
    IY = iy1-iy0+1
    tmp = max(IX, IY)
    # get the range such that it divisible by divdim (default 64) for GPU execution
    IXY = divdim * ((tmp+divdim-1)/divdim)
    div = (IXY-IX)/2
    # x
    ix0 -= div
    ix1 += (IXY-IX)-div
    # y
    div = (IXY-IY)/2
    iy0 -= div
    iy1 += (IXY-IY)-div
    # z
    tmp = (len(qz)-iz0+1)
    IZ = divdim * ((tmp+divdim-1)/divdim)
    iz0 -= IZ-tmp+1
    
    # save the trimming parameters in a dic
    trimpar = {'x':(ix0, ix1), 'y':(iy0, iy1), 'z':(iz0), 'fmax':fmax}

    # new dims (z,y,x)
    newdims = (imsum.shape[0]-iz0, iy1-iy0+1, ix1-ix0+1)
    imtrim = np.zeros((Nim,)+newdims, dtype=imdtype)
    imsumt = np.zeros(newdims, dtype=imdtype)
    # in case of needed padding (negative indx resulting above)
    # the absolute values are supposed to work like padding in case the indx are negative
    iz0s, iy0s, ix0s = iz0, iy0, ix0
    iz0t, iy0t, ix0t = 0,0,0
    if iz0<0: iz0s=0; iz0t = abs(iz0)
    if iy0<0: iy0s=0; iy0t = abs(iy0)
    if ix0<0: ix0s=0; ix0t = abs(ix0)

    # first trim the sum image
    imsumt[iz0t:, iy0t:, ix0t: ] = imsum[iz0s:, iy0s:iy1+1, ix0s:ix1+1]

    # new affine matrix for the scaled and trimmed image
    A = np.diag(sf*np.diag(affine))
    A[3,3] = 1
    A[0,3] = affine[0,3] - abs(affine[0,0])*sf*ix0
    A[1,3] = affine[1,3] + affine[1,1]*(imshape[1]-sf*iy1)
    A[2,3] = affine[2,3]

    # output dictionary
    dctout = {'affine': A, 'trimpar':trimpar, 'imsum':imsumt}

    # NIfTI image description (to be stored in the header)
    niidescr = 'trimm(x,y,z):' \
               + str(trimpar['x'])+',' \
               + str(trimpar['y'])+',' \
               + str((trimpar['z'],)) \
               + ';scale='+str(scale) \
               + ';fmx='+str(fmax)
    
    # store the sum image
    if store_avg:
        fsum = os.path.join(petudir, 'avg_trimmed-upsampled-scale-'+str(scale)+fcomment+'.nii.gz')
        imio.array2nii( imsumt[::-1,::-1,:], A, fsum, descrip=niidescr)
        if verbose:  print 'i> saved averaged image to:', fsum
        dctout['fsum'] = fsum

    # list of file names for the upsampled and trimmed images
    fpetu = []
    # perform the trimming and save the intermediate images if requested
    for i in range(Nim):

        # memory saving option, second time doing interpolation
        if memlim:
            im = ndi.interpolation.zoom(imin[i,:,:,:], (scale, scale, scale), order=int_order )
        else:
            im = imscl[i,:,:,:]

        # trim the scaled image
        imtrim[i, iz0t:, iy0t:, ix0t: ] = im[iz0s:, iy0s:iy1+1, ix0s:ix1+1]
        
        # save the up-sampled and trimmed PET images
        if store_img_intrmd:
            _frm = '_frm-'+str(i)
            _fstr = '_trimmed-upsampled-scale-'+str(scale) + _frm*(Nim>1) +fcomment
            fpetu.append( os.path.join(petudir, fnms[i]+_fstr+'.nii.gz') )
            imio.array2nii( imtrim[i,::-1,::-1,:], A, fpetu[i], descrip=niidescr)
            if verbose:  print 'i> saved upsampled PET image to:', fpetu[i]

    if store_img:
        _nfrm = '_nfrm-'+str(Nim)
        fim = os.path.join(petudir, 'final_trimmed-upsampled-scale-'+str(scale))+_nfrm*(Nim>1)+fcomment+'.nii.gz'
        imio.array2nii( np.squeeze(imtrim[:,::-1,::-1,:]), A, fim, descrip=niidescr)
        dctout['fim'] = fim

    # file names (with paths) for the intermediate PET images
    dctout['fimi'] = fpetu
    dctout['im'] = np.squeeze(imtrim)
    dctout['N'] = Nim
        
    return dctout




# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>


# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
def psf_general(vx_size=(1,1,1), fwhm=(5, 5, 6), hradius=8, scale=2):
    '''
    Separable kernels for convolution executed on the GPU device
    The outputted kernels are in this order: z, y, x
    '''
    xSig = (scale*fwhm[0]/vx_size[0]) / (2*(2*np.log(2))**.5)
    ySig = (scale*fwhm[1]/vx_size[1]) / (2*(2*np.log(2))**.5)
    zSig = (scale*fwhm[2]/vx_size[2]) / (2*(2*np.log(2))**.5)

    # get the separated kernels
    x = np.arange(-hradius,hradius+1)
    xKrnl = np.exp(-(x**2/(2*xSig**2)))
    yKrnl = np.exp(-(x**2/(2*ySig**2)))
    zKrnl = np.exp(-(x**2/(2*zSig**2)))

    # normalise kernels
    xKrnl /= np.sum(xKrnl)
    yKrnl /= np.sum(yKrnl)
    zKrnl /= np.sum(zKrnl)

    krnl = np.array([zKrnl, yKrnl, xKrnl], dtype=np.float32)

    # for checking if the normalisation worked
    # np.prod( np.sum(krnl,axis=1) )

    # return all kernels together
    return krnl
# ------------------------------------------------------------------------------------------------------

def psf_measured(scanner='mmr', scale=1):
    if scanner=='mmr':
        # file name for the mMR's PSF and chosen scale
        fnm = 'PSF-17_scl-'+str(int(scale))+'.npy'
        fpth = resource_filename('niftypet', 'auxdata')
        fdat = os.path.join(fpth, fnm)
        # cdir = os.path.dirname(resource_filename(__name__, __file__))
        # niftypet_dir = os.path.dirname(os.path.dirname(cdir))
        # fdat = os.path.join( os.path.join(niftypet_dir , 'auxdata' ), fnm)

        # transaxial and axial PSF
        Hxy, Hz = np.load(fdat)
        krnl = np.array([Hz, Hxy, Hxy], dtype=np.float32)
    else:
        raise NameError('e> only Siemens mMR is currently supported.')
    return krnl
# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>


# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
def iyang(imgIn, krnl, imgSeg, Cnt, itr=5):
    '''partial volume correction using iterative Yang method
    imgIn: input image which is blurred due to the PSF of the scanner
    krnl: shift invariant kernel of the PSF
    imgSeg: segmentation into regions starting with 0 (e.g., background) and then next integer numbers
    itr: number of iteration (default 5)
    '''
    dim = imgIn.shape
    m = np.int32(np.max(imgSeg))
    m_a = np.zeros(( m+1, itr ), dtype=np.float32)

    for jr in range(0,m+1): 
        m_a[jr, 0] = np.mean( imgIn[imgSeg==jr] )

    # init output image
    imgOut = np.copy(imgIn)

    # iterative Yang algorithm:
    for i in range(0, itr):
        if Cnt['VERBOSE']: print 'i> PVC Yang iteration =', i
        # piece-wise constant image
        imgPWC = imgOut
        imgPWC[imgPWC<0] = 0
        for jr in range(0,m+1):
            imgPWC[imgSeg==jr] = np.mean( imgPWC[imgSeg==jr] )
        # blur the piece-wise constant image
        imgSmo = np.zeros(imgIn.shape, dtype=np.float32)
        # convolution on GPU with separable kernel (x,y,z):
        improc.convolve(imgSmo, imgPWC, krnl, Cnt)
        # correction factors
        imgCrr = np.ones(dim, dtype=np.float32)
        imgCrr[imgSmo>0] = imgPWC[imgSmo>0] / imgSmo[imgSmo>0]
        imgOut = imgIn * imgCrr;
        for jr in range(0,m+1):
            m_a[jr, i] = np.mean( imgOut[imgSeg==jr] )

    return imgOut, m_a
# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>


# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
# G E T   P A R C E L L A T I O N S   F O R   P V C   A N D   R O I   E X T R A C T I O N
# ------------------------------------------------------------------------------------------------------
def pvc_iyang(petin, mridct, Cnt, pvcroi, krnl, outpath='', faff='', fcomment='', tool='nifty', itr=5):
    ''' Perform partial volume (PVC) correction of PET data (petin) using MRI data (mridct).
        The PVC method uses iterative Yang method.
        GPU based convolution is the key routine of the PVC. 
        Input:
        -------
        petin:  either a dictionary of image data, file name and affine transform,
                or a string of the path to the NIfTI file of the PET data.
        mridct: a dictionary of MRI data, including the T1w image, which can be given
                in DICOM (field 'T1dcm') or NIfTI (field 'T1nii').  The T1w image data
                is needed for co-registration to PET if affine is not given in the text
                file with its path in faff.
        Cnt:    a dictionary of paths for third-party tools:
                * dcm2niix: Cnt['DCM2NIIX']
                * niftyreg, resample: Cnt['RESPATH']
                * niftyreg, rigid-reg: Cnt['REGPATH']
                * verbose mode on/off: Cnt['VERBOSE'] = True/False
        pvcroi: list of regions (also a list) with number label to distinguish
                the parcellations.  The numbers correspond to the image values
                of the parcellated T1w image.  E.g.:
                pvcroi = [
                    [36], # ROI 1 (single parcellation region)
                    [35], # ROI 2
                    [39, 40, 72, 73, 74], # ROI 3 (region consisting of multiple parcellation regions)
                    ...
                ]
        kernel: the point spread function (PSF) specific for the camera and the object.  
                It is given as a 3x17 matrix, a 17-element kernel for each dimension (x,y,z).
                It is used in the GPU-based convolution using separable kernels.
        outpath:path to the output of the resulting PVC images
        faff:   a text file of the affine transformations needed to get the MRI into PET space.
                If not provided, it will be obtained from the performed rigid transformation.
                For this the MR T1w image is required.  If faff and T1w image are not provided,
                it will results in exception/error.
        fcomment:a string used in naming the produced files, helpful for distinguishing them.
        tool:   co-registration tool.  By default it is NiftyReg, but SPM is also 
                possible (needs Matlab engine and more validation)
        itr:    number of iterations used by the PVC.  5-10 should be enough (5 default)
    '''

    # get all the input image properties
    if isinstance(petin, dict):
        im = imdic['im']
        fpet = imdic['fpet']
        B = imdic['affine']
    elif isinstance(petin, basestring) and os.path.isfile(petin):
        im = imio.getnii(petin)
        fpet = petin
        B = imio.getnii_affine(petin)
    
    if im.ndim!=3:
        raise IndexError('Only 3D images are expected in this method of partial volume correction.')

    # check if brain parcellations exist in NIfTI format
    if not os.path.isfile(mridct['T1lbl']):
        raise NameError('MissingLabels')

    # path to labels of brain parcellations in NIfTI format
    lbpth = os.path.split(mridct['T1lbl'])

    #==================================================================
    #if affine transf. (faff) is given then take the T1 and resample it too.
    if isinstance(faff, basestring) and not os.path.isfile(faff):
        # faff is not given; get it by running the affine; get T1w to PET space
        faff = mr2pet_rigid(fpet, mridct, Cnt, outpath=outpath, fcomment=fcomment)

    # establish the output folder
    if outpath=='':        
        prcl_dir = os.path.dirname(mridct['T1lbl'])
    else:
        prcl_dir = outpath
        imio.create_dir(prcl_dir)

    # resample the GIF T1/labels to upsampled PET
    if tool=='nifty':
        # file name of the parcellation (GIF-based) upsampled to PET
        fgt1u = os.path.join(prcl_dir, os.path.basename(mridct['T1lbl']).split('.')[0]+'_registered_trimmed'+fcomment+'.nii.gz')
        if os.path.isfile( Cnt['RESPATH'] ):
            cmd = [Cnt['RESPATH'],  '-ref', fpet,  '-flo', mridct['T1lbl'],
                   '-trans', faff, '-res', fgt1u, '-inter', '0']
            if not Cnt['VERBOSE']: cmd.append('-voff')
            call(cmd)
        else:
            print 'e> path to resampling executable is incorrect!'
            sys.exit()
        
    elif tool=='spm':
        fgt1u = nipet.img.prc.spm_resample(  fpet, mridct['T1lbl'], faff, 
                                                intrp=0, dirout=prcl_dir, r_prefix='r_trimmed_'+fcomment, 
                                                del_ref_uncmpr=True, del_flo_uncmpr=True, del_out_uncmpr=True)

    #==================================================================

    # Get the labels before resampling to PET space, so that the regions can be separated for the PET space
    nilb = nib.load(mridct['T1lbl'])
    A = nilb.get_sform()
    imlb = nilb.get_data()

    # ===============================================================
    # get the segmentation/parcellation for PVC
    # create and reset the image for parcellation
    imgroi = np.copy(imlb);  imgroi[:] = 0
    # number of segments, without the background
    nSeg = len(pvcroi)
    # create the image of numbered parcellations/segmentations
    for k in range(nSeg):
        for m in pvcroi[k]:
            imgroi[imlb==m] = k+1
    # create the NIfTI image
    froi1 = os.path.join(prcl_dir, lbpth[1].split('.')[0][:8]+'_pvcroi_'+fcomment+'.nii.gz')
    niiSeg = nib.Nifti1Image(imgroi, A)
    niiSeg.header['cal_max'] = np.max(imgroi)
    niiSeg.header['cal_min'] = 0.
    nib.save(niiSeg, froi1)
    if tool=='nifty':
        froi2 = os.path.join(prcl_dir, lbpth[1].split('.')[0][:8]+'_pvcroi_registered_trimmed_'+fcomment+'.nii.gz')
        if os.path.isfile( Cnt['RESPATH'] ):
            cmd = [Cnt['RESPATH'], '-ref', fpet, '-flo', froi1, '-trans', faff,  '-res', froi2,  '-inter', '0']
            if not Cnt['VERBOSE']: cmd.append('-voff')       
            call(cmd)
        else:
            print 'e> path to resampling executable is incorrect!'
            sys.exit()
    elif tool=='spm':
        froi2 = spm_resample(
            fpet, froi1, faff, 
            intrp=0, dirout=prcl_dir, r_prefix='r_'+fcomment+'pvcroi_registered_', 
            del_ref_uncmpr=True, del_flo_uncmpr=True, del_out_uncmpr=True
        )
    # -----------------------------------
    imgroi2 = imio.getnii(froi2)
    # run iterative Yang PVC
    imgpvc, m_a = iyang(im, krnl, imgroi2, Cnt, itr=itr)
    fpvc = os.path.join( os.path.split(fpet)[0],
                         os.path.split(fpet)[1].split('.')[0]+'_PVC'+fcomment+'.nii.gz')
    imio.array2nii( imgpvc[::-1,::-1,:], B, fpvc, descrip='pvc=iY') 
    # ===============================================================
    return {'fpet':fpvc, 'im':imgpvc, 'froi':froi2, 'imroi':imgroi2, 'faff':faff}


# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
# P E T 2 P E T   R E G I S T R A T I O N
# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
def pet2pet_rigid(fref, fflo, Cnt, outpath='', rmsk=True, rfwhm=1.5, rthrsh=0.05, pi=50, pv=50, smof=0, smor=0):

    #create a folder for PET images registered to ref PET
    if outpath=='':
        outpath = os.path.dirname(fflo)
    odir = os.path.join(outpath,'PET2PET')
    nipet.mmraux.create_dir(odir)

    if rmsk:
        fimdir = os.path.join(odir, 'tmp')
        nipet.mmraux.create_dir(fimdir)
        fmsk = os.path.join(fimdir, 'rmask.nii.gz')

        smoim = ndi.filters.gaussian_filter(nimpa.prc.getnii(fref),
                                            nipet.mmraux.fwhm2sig(rfwhm), mode='mirror')
        thrsh = rthrsh*smoim.max()
        immsk = np.int8(smoim>thrsh)
        for iz in range(immsk.shape[0]):
            for iy in range(immsk.shape[1]):
                ix0 = np.argmax(immsk[iz,iy,:]>0)
                ix1 = immsk.shape[2] - np.argmax(immsk[iz,iy,::-1]>0)
                if (ix1-ix0) > immsk.shape[2]-10: continue
                immsk[iz,iy,ix0:ix1] = 1
        imio.array2nii( immsk[::-1,::-1,:], imio.getnii_affine(fref), fmsk)

    # output in register with ref PET
    fout = os.path.join(odir, 'PET-r-to-'+os.path.basename(fref).split('.')[0]+'.nii.gz')
    # text file for the affine transform T1w->PET
    faff   = os.path.join(odir, 'affine-PET-r-to-'+os.path.basename(fref).split('.')[0]+'.txt')  
    # call the registration routine
    if os.path.isfile( Cnt['REGPATH'] ):
        cmd = [Cnt['REGPATH'],
             '-ref', fref,
             '-flo', fflo,
             '-rigOnly', '-speeeeed',
             '-aff', faff,
             '-pi', str(pi),
             '-pv', str(pv),
             '-smooF', str(smof),
             '-smooR', str(smor),
             '-res', fout]
        if rmsk: 
            cmd.append('-rmask')
            cmd.append(fmsk)
        if not Cnt['VERBOSE']: cmd.append('-voff')
        print cmd
        call(cmd)
    else:
        print 'e> path to registration executable is incorrect!'
        raise StandardError('No registration executable found')
        
    return faff, fout


# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
def mr2pet_rigid(fpet, mridct, Cnt, outpath='', fcomment='', rmsk=True, rfwhm=1.5, rthrsh=0.05, pi=50, pv=50, smof=0, smor=0):

    # create output path if given
    if outpath!='':
        nipet.mmraux.create_dir(outpath)

    # --- MR T1w
    if 'T1nii' in mridct and os.path.isfile(mridct['T1nii']):
        ft1w = mridct['T1nii']
    elif 'T1bc' in mridct and os.path.isfile(mridct['T1bc']):
        ft1w = mridct['T1bc']
    elif 'T1dcm' in mridct and os.path.exists(mridct['MRT1W']):
        # create file name for the converted NIfTI image
        fnii = 'converted'
        call( [ Cnt['DCM2NIIX'], '-f', fnii, mridct['T1nii'] ] )
        ft1nii = glob.glob( os.path.join(mridct['T1nii'], '*converted*.nii*') )
        ft1w = ft1nii[0]
    else:
        print 'e> disaster: no T1w image!'
        sys.exit()

    if rmsk:
        if outpath!='':
            fimdir = os.path.join( outpath, os.path.join('T1w2PET','tmp'))
        else:
            fimdir = os.path.join( os.path.basename(ft1w), 'tmp')

        nipet.mmraux.create_dir(fimdir)
        fmsk = os.path.join(fimdir, 'rmask.nii.gz')
        imdct = imio.getnii(fpet, output='all')
        smoim = ndi.filters.gaussian_filter(imdct['im'],
                                            imio.fwhm2sig(rfwhm, voxsize=imdct['affine'][0,0]), mode='mirror')
        thrsh = rthrsh*smoim.max()
        immsk = np.int8(smoim>thrsh)
        for iz in range(immsk.shape[0]):
            for iy in range(immsk.shape[1]):
                ix0 = np.argmax(immsk[iz,iy,:]>0)
                ix1 = immsk.shape[2] - np.argmax(immsk[iz,iy,::-1]>0)
                if (ix1-ix0) > immsk.shape[2]-10: continue
                immsk[iz,iy,ix0:ix1] = 1
        imio.array2nii( immsk[::-1,::-1,:], imio.getnii_affine(fpet), fmsk)

    #create a folder for MR images registered to PET
    if outpath!='':
        mrodir = os.path.join(outpath,'T1w2PET')
    else:
        mrodir = os.path.join(os.path.dirname(ft1w),'mr2pet')
    nipet.mmraux.create_dir(mrodir)

    # if provided, separate the comment with underscore
    if fcomment!='': fcomment = '_'+fcomment
    # output for the T1w in register with PET
    ft1out = os.path.join(mrodir, 'T1w-r-to-'+os.path.basename(fpet).split('.')[0]+fcomment+'.nii.gz')
    # text file for the affine transform T1w->PET
    faff   = os.path.join(mrodir, 'affine-T1w-r-to-'+os.path.basename(fpet).split('.')[0]+fcomment+'.txt')  
    # call the registration routine
    if os.path.isfile( Cnt['REGPATH'] ):
        cmd = [Cnt['REGPATH'],
             '-ref', fpet,
             '-flo', ft1w,
             '-rigOnly', '-speeeeed',
             '-aff', faff,
             '-pi', str(pi),
             '-pv', str(pv),
             '-smooF', str(smof),
             '-smooR', str(smor),
             '-res', ft1out]
        if rmsk: 
            cmd.append('-rmask')
            cmd.append(fmsk)
        if 'VERBOSE' in Cnt and not Cnt['VERBOSE']: cmd.append('-voff')
        print cmd
        call(cmd)
    else:
        print 'e> path to registration executable is incorrect!'
        sys.exit()
        
    return faff


#---- SPM ----
def spm_resample(imref, imflo, m, intrp=1, dirout='', r_prefix='r_', del_ref_uncmpr=False, del_flo_uncmpr=False, del_out_uncmpr=False):
    import matlab.engine
    from pkg_resources import resource_filename
    # start matlab engine
    eng = matlab.engine.start_matlab()
    # add path to SPM matlab file
    spmpth = resource_filename(__name__, 'spm')
    eng.addpath(spmpth, nargout=0)

    # decompress if necessary 
    if imref[-3:]=='.gz':
        imrefu = imio.nii_ugzip(imref)
    else:
        imrefu = imref
    if imflo[-3:]=='.gz': 
        imflou = imio.nii_ugzip(imflo)
    else:
        imflou = imflo

    # run the matlab SPM resampling
    mm = eng.spm_resample(imrefu, imflou, matlab.single(m.tolist()), intrp, r_prefix)

    # delete the uncomressed
    if del_ref_uncmpr:  os.remove(imrefu)
    if del_flo_uncmpr:  os.remove(imflou)

    # compress the output
    split = os.path.split(imflou)
    fim = os.path.join(split[0], r_prefix+split[1])
    imio.nii_gzip(fim)
    if del_out_uncmpr: os.remove(fim)

    if dirout!='':
        # move to the output dir
        fout = os.path.join(dirout, r_prefix+split[1]+'.gz')
        os.rename(fim+'.gz', fout)
    else:
        fout = fim+'.gz'

    return fout


def spm_coreg(imref, imflo, del_uncmpr=False):
    import matlab.engine
    from pkg_resources import resource_filename
    # start matlab engine
    eng = matlab.engine.start_matlab()
    # add path to SPM matlab file
    spmpth = resource_filename(__name__, 'spm')
    eng.addpath(spmpth, nargout=0)

    # decompress if necessary 
    if imref[-3:]=='.gz':
        imrefu = imio.nii_ugzip(imref)
    else:
        imrefu = imref
    if imflo[-3:]=='.gz': 
        imflou = imio.nii_ugzip(imflo)
    else:
        imflou = imflo

    # run the matlab SPM coregistration
    M = eng.spm_mr2pet(imrefu, imflou)
    # get the affine matrix
    m = np.array(M._data.tolist())
    m = m.reshape(4,4).T
    # delete the uncompressed files
    if del_uncmpr:
        if imref[-3:]=='.gz': os.remove(imrefu)
        if imflo[-3:]=='.gz': os.remove(imflou)

    return m

#---- FSL ----
def fsl_coreg(imref, imflo, faff, costfun='normmi', dof=6):

    cmd = [ 'fsl5.0-flirt',
            '-cost', costfun,
            '-dof', str(dof),
            '-omat', faff,
            '-in', imflo,
            '-ref', imref]
    call(cmd)

    # convert hex parameters to dec
    aff = np.loadtxt(faff)
    # faffd = faff[:-4]+'d.mat'
    np.savetxt(faff, aff)
    return aff


def fsl_res(imout, imref, imflo, faff, interp=1):

    if interp==1:
        interpolation = 'trilinear'
    elif interp==0:
        interpolation = 'nearestneighbour'

    cmd = [ 'fsl5.0-flirt',
            '-in', imflo,
            '-ref', imref,
            '-out', imout,
            '-applyxfm', '-init', faff,
            '-interp', interpolation]
    call(cmd)






def dcm2im(fpth):
    '''Get the DICOM files from 'fpth' into an image with the affine transformation.'''

    SZ0 = len([d for d in os.listdir(fpth) if d.endswith(".dcm")])
    if SZ0<1:
        print 'e> no DICOM images in the specified path.'
        sys.exit()

    #patient position
    dhdr = dcm.read_file(os.path.join(fpth, os.listdir(fpth)[0]))
    if [0x018, 0x5100] in dhdr:
        ornt = dhdr[0x18,0x5100].value
    else:
        ornt = 'unkonwn'

    # image position 
    P = np.zeros((SZ0,3), dtype=np.float64)
    #image orientation
    Orn = np.zeros((SZ0,6), dtype=np.float64)
    #xy resolution
    R = np.zeros((SZ0,2), dtype=np.float64)
    #slice thickness
    S = np.zeros((SZ0,1), dtype=np.float64)
    #slope and intercept
    SI = np.ones((SZ0,2), dtype=np.float64)
    SI[:,1] = 0

    #image data as an list of array for now
    IM = []

    c = 0;
    for d in os.listdir(fpth):
        if d.endswith(".dcm"):
            dhdr = dcm.read_file(fpth+'/'+d)
            P[c,:] = np.array([float(f) for f in dhdr[0x20,0x32].value])
            Orn[c,:] = np.array([float(f) for f in dhdr[0x20,0x37].value])
            R[c,:] = np.array([float(f) for f in dhdr[0x28,0x30].value])
            S[c,:] = float(dhdr[0x18,0x50].value)
            if [0x28,0x1053] in dhdr and [0x28,0x1052] in dhdr:
                SI[c,0] = float(dhdr[0x28,0x1053].value)
                SI[c,1] = float(dhdr[0x28,0x1052].value)
            IM.append(dhdr.pixel_array)
            c += 1


    #check if orientation/resolution is the same for all slices
    if np.sum(Orn-Orn[0,:]) > 1e-6:
        print 'e> varying orientation for slices'
    else:
        Orn = Orn[0,:]
    if np.sum(R-R[0,:]) > 1e-6:
        print 'e> varying resolution for slices'
    else:
        R = R[0,:]

    # Patient Position
    patpos = dhdr[0x18,0x5100].value
    # Rows and Columns
    SZ2 = dhdr[0x28,0x10].value
    SZ1 = dhdr[0x28,0x11].value
    # image resolution
    SZ_VX2 = R[0]
    SZ_VX1 = R[1]

    #now sort the images along k-dimension
    k = np.argmin(abs(Orn[:3]+Orn[3:]))
    #sorted indeces
    si = np.argsort(P[:,k])
    Pos = np.zeros(P.shape, dtype=np.float64)
    im = np.zeros((SZ0, SZ1, SZ2 ), dtype=np.float32)

    #check if the dementions are in agreement (the pixel array could be transposed...)
    if IM[0].shape[0]==SZ1:
        for i in range(SZ0):
            im[i,:,:] = IM[si[i]]*SI[si[i],0] + SI[si[i],1]
            Pos[i,:] = P[si[i]]
    else:
        for i in range(SZ0):
            im[i,:,:] = IM[si[i]].T * SI[si[i],0] + SI[si[i],1]
            Pos[i,:] = P[si[i]]

    # proper slice thickness
    Zz = (P[si[-1],2] - P[si[0],2])/(SZ0-1)
    Zy = (P[si[-1],1] - P[si[0],1])/(SZ0-1)
    Zx = (P[si[-1],0] - P[si[0],0])/(SZ0-1)
    

    # dictionary for affine and image size for the image
    A = {
        'AFFINE':np.array([[SZ_VX2*Orn[0], SZ_VX1*Orn[3], Zx, Pos[0,0]],
                           [SZ_VX2*Orn[1], SZ_VX1*Orn[4], Zy, Pos[0,1]],
                           [SZ_VX2*Orn[2], SZ_VX1*Orn[5], Zz, Pos[0,2]],
                           [0., 0., 0., 1.]]),
        'SHAPE':(SZ0, SZ1, SZ2)
    }

    #the returned image is already scaled according to the dcm header
    return im, A, ornt


# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
# R O I   E X T R A C T I O N
# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>

def roi_extraction(imdic, amyroi, datain, Cnt, use_stored=False):
    '''
    Extracting ROI values of upsampled PET.  If not provided it will be upsampled.
    imdic: dictionary of all image parts (image, path to file and affine transformation)    
     > fpet: file name for quantitative PET
     > faff: file name and path to the affine (rigid) transformations, most likely for upscaled PET
    amyroi: module with ROI definitions in numbers
    datain: dictionary of all the input data
    '''

    fpet = imdic['fpet']
    faff = imdic['faff']
    im   = imdic['im']

    if Cnt['VERBOSE']: print 'i> extracting ROI values'

    # split the labels path
    lbpth = os.path.split(datain['T1lbl'])
    # # MR T1w NIfTI
    # if os.path.isfile(datain['T1nii']):
    #     ft1w = datain['T1nii']
    # elif os.path.isfile(datain['T1bc']):
    #     ft1w = datain['T1nii']
    # else:
    #     print 'e> no T1w NIfTI!'
    #     sys.exit()

    prcl_dir = os.path.dirname(datain['T1lbl'])

    dscrp = imio.getnii_descr(fpet)
    impvc = False
    if 'pvc' in dscrp.keys():       
        impvc = True
        if Cnt['VERBOSE']: print 'i> the provided image is partial volume corrected.'

    #------------------------------------------------------------------------------------------------------------  
    # next steps after having sorted out the upsampled input image and the rigid transformation for the T1w -> PET:

    # Get the labels before resampling to PET space so that the regions can be separated (more difficult to separate after resampling)
    if os.path.isfile(datain['T1lbl']):
        nilb = nib.load(datain['T1lbl'])
        A = nilb.get_sform()
        imlb = nilb.get_data()
    else:
        print 'e> parcellation label image not present!'
        sys.exit()

    # ===============================================================
    # get the segmentation/parcellation by creating an image for ROI extraction
    # (still in the original T1 orientation)
    roi_img = np.copy(imlb)

    # sum of all voxel values in any given ROI
    roisum = {}
    # sum of the mask values <0,1> used for getting the mean value of any ROI
    roimsk = {}
    for k in amyroi.rois.keys():
        roisum[k] = []
        roimsk[k] = []

    # extract the ROI values from PV corrected and uncorrected PET images
    for k in amyroi.rois.keys():
        roi_img[:] = 0
        for i in amyroi.rois[k]:
            roi_img[imlb==i] = 1.
        # now save the image mask to nii file <froi1>
        froi1 = os.path.join(prcl_dir, lbpth[1].split('.')[0][:8]+'_'+k+'.nii.gz')
        nii_roi = nib.Nifti1Image(roi_img, A)
        nii_roi.header['cal_max'] = 1.
        nii_roi.header['cal_min'] = 0.
        nib.save(nii_roi, froi1)
        # file name for the resampled ROI to PET space
        froi2 = os.path.join(prcl_dir, os.path.basename(datain['T1lbl']).split('.')[0]+ '_toPET_'+k+'.nii.gz')
        if not use_stored and os.path.isfile( Cnt['RESPATH'] ):
            cmd = [Cnt['RESPATH'],
                '-ref', fpet,
                '-flo', froi1,
                '-trans', faff,
                '-res', froi2]#,
                #'-pad', '0']
            if not Cnt['VERBOSE']: cmd.append('-voff') 
            call(cmd)
        # get the resampled ROI mask image
        rmsk = imio.getnii(froi2)
        rmsk[rmsk>1.] = 1.
        rmsk[rmsk<0.] = 0.

        # erode the cerebral white matter region if no PVC image
        if k=='wm' and not impvc:
            if Cnt['VERBOSE']: print 'i> eroding white matter as PET image not partial volume corrected.'
            nilb = nib.load(froi2)
            B = nilb.get_sform()
            tmp = ndi.filters.gaussian_filter(rmsk, nipet.mmraux.fwhm2sig(12./10), mode='mirror')
            rmsk = np.float32(tmp>0.85)
            froi3 = os.path.join(prcl_dir, os.path.basename(datain['T1lbl']).split('.')[0]+ '_toPET_'+k+'_eroded.nii.gz')
            nipet.img.mmrimg.savenii(rmsk, froi3, B, Cnt)

        # ROI value and mask sums:
        rvsum  = np.sum(im*rmsk)
        rmsum  = np.sum(rmsk)
        roisum[k].append( rvsum )
        roimsk[k].append( rmsum )
        if Cnt['VERBOSE']: 
            print ''
            print '================================================================'
            print 'i> ROI extracted:', k
            print '   > value sum:', rvsum
            print '   > # voxels :', rmsum
            print '================================================================'
    # --------------------------------------------------------------

    return roisum, roimsk



def roi_extraction_spm(imdic, amyroi, datain, Cnt, dirout, r_prefix='r_', use_stored=False):

    fpet = imdic['fpet']
    M  = imdic['affine'] # matrix with affine parameters
    im   = imdic['im']

    if Cnt['VERBOSE']: print 'i> extracting ROI values'

    # split the labels path
    lbpth = os.path.split(datain['T1lbl'])
    prcl_dir = os.path.dirname(datain['T1lbl'])

    dscrp = nimpa.prc.getnii_descr(fpet)
    impvc = False
    if 'pvc' in dscrp.keys():       
        impvc = True
        if Cnt['VERBOSE']: print 'i> the provided image is partial volume corrected.'

    #------------------------------------------------------------------------------------------------------------  
    # next steps after having sorted out the upsampled input image and the rigid transformation for the T1w -> PET:

    # Get the labels before resampling to PET space so that the regions can be separated (more difficult to separate after resampling)
    if os.path.isfile(datain['T1lbl']):
        if datain['T1lbl'][-3:]=='.gz':
            flbl = imio.nii_ugzip(datain['T1lbl'])
        else:
            flbl = datain['T1lbl']
    else:
        print 'e> parcellation label image not present!'
        sys.exit()

    # ===============================================================
    # resample the labels to upsampled PET
    if fpet[-3:]=='.gz': 
        fpet_ = imio.nii_ugzip(fpet)
    else:
        fpet_ = fpet
    # get file of coregistered labels to upsampled PET
    flblu = nipet.img.prc.spm_resample(fpet_, flbl, M, intrp=0, dirout=dirout, r_prefix=r_prefix, del_flo_uncmpr=True, del_out_uncmpr=True)

    imlbl = imio.getnii(flblu)

    # get the segmentation/parcellation by creating an image for ROI extraction
    roi_img = np.copy(imlbl)

    # sum of all voxel values in any given ROI
    roisum = {}
    # sum of the mask values <0,1> used for getting the mean value of any ROI
    roimsk = {}
    for k in amyroi.rois.keys():
        roisum[k] = []
        roimsk[k] = []

    # extract the ROI values from PV corrected and uncorrected PET images
    for k in amyroi.rois.keys():
        roi_img[:] = 0
        for i in amyroi.rois[k]:
            roi_img[imlbl==i] = 1.

        # ROI value and mask sums:
        rvsum  = np.sum(im*roi_img)
        rmsum  = np.sum(roi_img)
        roisum[k].append( rvsum )
        roimsk[k].append( rmsum )
        if Cnt['VERBOSE']: 
            print ''
            print '================================================================'
            print 'i> ROI extracted:', k
            print '   > value sum:', rvsum
            print '   > # voxels :', rmsum
            print '================================================================'
    # --------------------------------------------------------------

    return roisum, roimsk



# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
# U T E  +   p C T    B O T S T R A P   R E C O N S T R U C T I O N   &   A N A L Y S I S
# <><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><><>
def btproi(datain, amyroi, txLUT, axLUT, Cnt, Bitr = 50, upscale=True, t0=0, t1=0, itr=4, fwhm=0., fcomment='', int_order=0):

    # ff not bootstrap then do only one reconstruction
    if Cnt['BTP']==0: Bitr = 1

    # array for all bootstrap/normal SUVr's extracted from multiple/single reconstruction(s) and for different reference regions
    # SUVr = { 'ute':{'nopvc':[], 'pvc':[]}, 'pct':{'nopvc':[], 'pvc':[]} }
    # SUVr['ute']['nopvc'] = np.zeros((Bitr, len(amyroi.refi), len(amyroi.roii)), dtype=np.float32)
    # SUVr['ute']['pvc']   = np.zeros((Bitr, len(amyroi.refi), len(amyroi.roii)), dtype=np.float32)
    # SUVr['pct']['nopvc'] = np.zeros((Bitr, len(amyroi.refi), len(amyroi.roii)), dtype=np.float32)
    # SUVr['pct']['pvc']   = np.zeros((Bitr, len(amyroi.refi), len(amyroi.roii)), dtype=np.float32)

    btpout = namedtuple('btpout',  'Rpct,  Rpctpvc,  Rute,  Rutepvc, \
                                    M1pct, M1pctpvc, M1ute, M1utepvc, \
                                    M2pct, M2pctpvc, M2ute, M2utepvc, Itrp, Itru')
    btpout.Rpct    = np.zeros((Bitr, len(amyroi.refi), len(amyroi.roii)), dtype=np.float32)
    btpout.Rpctpvc = np.zeros((Bitr, len(amyroi.refi), len(amyroi.roii)), dtype=np.float32)
    btpout.Rute    = np.zeros((Bitr, len(amyroi.refi), len(amyroi.roii)), dtype=np.float32)
    btpout.Rutepvc = np.zeros((Bitr, len(amyroi.refi), len(amyroi.roii)), dtype=np.float32)

    btpout.Itrp = 0
    btpout.Itru = 0

    for bitr in range(Bitr):
        if Cnt['BTP']==0:
            cmmnt = '(P)'
        else:
            cmmnt = '(b'+str(bitr)+')'
        # perform reconstruction with UTE and pCT mu-maps while aligning the pCT/T1w to PET-UTE
        recout = nipet.img.prc.osemduo(datain, txLUT, axLUT, Cnt, t0=t0, t1=t1, itr=itr, fcomment=fcomment+cmmnt, use_stored=True)
        
        # get the PSF kernel for PVC
        krnlPSF = nipet.img.mmrimg.getPSFmeasured()
        # trim PET and upsample.  The trimming is done based on the first file in the list below, i.e., recon with pCT
        imudic = nipet.img.prc.trimPET( datain, Cnt, fpets=[recout.fpct, recout.fute], scale=2, 
                                        fcomment='trim_'+fcomment, int_order=int_order)
        # create separate dictionary for UTE and pCT reconstructions
        imupct = {'im':imudic['im'][0,:,:,:], 'fpet':imudic['fpet'][0], 'affine':imudic['affine']} 
        imuute = {'im':imudic['im'][1,:,:,:], 'fpet':imudic['fpet'][1], 'affine':imudic['affine']}

        if bitr==0 and Cnt['BTP']>0:
            # initialise the online variance images in this first iteration
            btpout.M1pct   = np.zeros(imupct['im'].shape, dtype=np.float32)
            btpout.M1pctpvc= np.zeros(imupct['im'].shape, dtype=np.float32)
            btpout.M1ute   = np.zeros(imuute['im'].shape, dtype=np.float32)
            btpout.M1utepvc= np.zeros(imuute['im'].shape, dtype=np.float32)
            btpout.M2pct   = np.zeros(imupct['im'].shape, dtype=np.float32)
            btpout.M2pctpvc= np.zeros(imupct['im'].shape, dtype=np.float32)
            btpout.M2ute   = np.zeros(imuute['im'].shape, dtype=np.float32)
            btpout.M2utepvc= np.zeros(imuute['im'].shape, dtype=np.float32)
        
        #-----------------------------
        # PCT ANALYSIS
        if Cnt['BTP']==0:
            cmmnt = '(pct-P)'
        else:
            cmmnt = '(pct-b'+str(bitr)+')'
        # perform PVC, iterative Yang
        pvcdic = nipet.img.prc.pvc_iYang(datain, Cnt, imupct, amyroi, krnlPSF, faffu=recout.faff, fcomment=cmmnt)
        # the rigid transformation is the same for PVC corrected and uncorrected images
        imupct['faff'] = pvcdic['faff']
        # and now for PVC image
        roisum, roimsk = nipet.img.prc.roi_extraction(pvcdic, amyroi, datain, Cnt, use_stored=False)
        for i in range(len(amyroi.refi)):
            refc = np.array(roisum[amyroi.refi[i]]) / np.array(roimsk[amyroi.refi[i]])
            for j in range(len(amyroi.roii)):
                btpout.Rpctpvc[bitr,i,j] = ( np.array(roisum[amyroi.roii[j]]) / np.array(roimsk[amyroi.roii[j]]) ) / refc
        # extract the ROI values from uncorrected image
        roisum, roimsk = nipet.img.prc.roi_extraction(imupct, amyroi, datain, Cnt, use_stored=True)
        for i in range(len(amyroi.refi)):
            ref = np.array(roisum[amyroi.refi[i]]) / np.array(roimsk[amyroi.refi[i]])
            for j in range(len(amyroi.roii)):
                btpout.Rpct[bitr,i,j] = ( np.array(roisum[amyroi.roii[j]]) / np.array(roimsk[amyroi.roii[j]]) ) / ref
        # calculate variance online
        if Cnt['BTP']>0 and imupct['im'].shape==btpout.M1pct.shape:
            nipet.mmr_auxe.varon(btpout.M1pct,    btpout.M2pct,    imupct['im']/ref , btpout.Itrp, Cnt)
            nipet.mmr_auxe.varon(btpout.M1pctpvc, btpout.M2pctpvc, pvcdic['im']/refc, btpout.Itrp, Cnt)
            btpout.Itrp += 1
        else:
            print 'e> Omitting on-line variance calculations. Check the shape of images.'


        #-----------------------------
        # UTE ANALYSIS
        if Cnt['BTP']==0:
            cmmnt = '(ute-P)'
        else:
            cmmnt = '(ute-b'+str(bitr)+')' 
        # perform PVC, iterative Yang
        pvcdic = nipet.img.prc.pvc_iYang(datain, Cnt, imuute, amyroi, krnlPSF, faffu=recout.faff, fcomment=cmmnt)
        # the rigid transformation is the same for PVC corrected and uncorrected images
        imuute['faff'] = pvcdic['faff']
        # extract the ROI values from uncorrected image
        roisum, roimsk = nipet.img.prc.roi_extraction(imuute, amyroi, datain, Cnt, use_stored=False)
        for i in range(len(amyroi.refi)):
            ref = np.array(roisum[amyroi.refi[i]]) / np.array(roimsk[amyroi.refi[i]])
            for j in range(len(amyroi.roii)):
                btpout.Rute[bitr,i,j] = ( np.array(roisum[amyroi.roii[j]]) / np.array(roimsk[amyroi.roii[j]]) ) / ref
        # and now for PVC image
        roisum, roimsk = nipet.img.prc.roi_extraction(pvcdic, amyroi, datain, Cnt, use_stored=True)
        for i in range(len(amyroi.refi)):
            ref = np.array(roisum[amyroi.refi[i]]) / np.array(roimsk[amyroi.refi[i]])
            for j in range(len(amyroi.roii)):
                btpout.Rutepvc[bitr,i,j] = ( np.array(roisum[amyroi.roii[j]]) / np.array(roimsk[amyroi.roii[j]]) ) / ref
        # calculate variance online
        if Cnt['BTP']>0 and imuute['im'].shape==btpout.M1ute.shape:
            nipet.mmr_auxe.varon(btpout.M1ute,    btpout.M2ute,    imuute['im'], btpout.Itru, Cnt)
            nipet.mmr_auxe.varon(btpout.M1utepvc, btpout.M2utepvc, pvcdic['im'], btpout.Itru, Cnt)
            btpout.Itru += 1
        else:
            print 'e> different shape encountered in online variance calculations (UTE).  Omitting.'

    return btpout
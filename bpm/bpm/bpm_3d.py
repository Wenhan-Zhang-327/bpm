"""the main method for beam propagation in refractive media"""

import numpy as np

from gputools import OCLArray, OCLImage, OCLProgram, get_device
from gputools import fft, fft_plan
from gputools import OCLReductionKernel

from bpm.utils import StopWatch

from scipy.ndimage.interpolation import zoom

#this is the main method to calculate everything

def memory_usage():
    # return the memory usage in MB
    import psutil
    import os
    import resource
    mem1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1000.
    process = psutil.Process(os.getpid())
    mem2 = process.memory_info()[0] / float(2 ** 20)
    return np.round(mem1), np.round(mem2)


def absPath(myPath):
    import sys
    import os
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
        return os.path.join(base_path, os.path.basename(myPath))
    except Exception:
        base_path = os.path.abspath(os.path.dirname(__file__))
        return os.path.join(base_path, myPath)


def bpm_3d(size,
           units,
           lam = .5,
           u0 = None, dn = None,
           subsample = 1,
           n_volumes = 1,
           n0 = 1.,
           return_scattering = False,
           return_g = False,
           return_full = True,
           return_field = True,
           absorbing_width = 0,
           use_fresnel_approx = False,
           scattering_plane_ind = 0,
           store_dn_as_half = False,
           mean_mode = "weighted"):
    """
    simulates the propagation of monochromativ wave of wavelength lam with initial conditions u0 along z in a media filled with dn

    size     -    the dimension of the image to be calulcated  in pixels (Nx,Ny,Nz)
    units    -    the unit lengths of each dimensions in microns
    lam      -    the wavelength
    u0       -    the initial field distribution, if u0 = None an incident  plane wave is assumed
    dn       -    the refractive index of the medium (can be complex)
    n0       -    refractive index of surrounding medium
    return_full - if True, returns the complex field in volume otherwise only last plane
    return_field -
    return_last_plane -

    mean_mode = "simple" or "weighted" or "ignore"
    """


    if n_volumes ==1:
        return _bpm_3d(size, units,
                       lam = lam,
                       u0 = u0, dn = dn,
                       subsample = subsample,
                       n0 = n0,
                       return_scattering = return_scattering,
                       return_full = return_full,
                       return_field = return_field,
                       return_g = return_g,
                       absorbing_width = absorbing_width,
                       use_fresnel_approx = use_fresnel_approx,
                       scattering_plane_ind =  scattering_plane_ind,
                       store_dn_as_half = store_dn_as_half,
                       mean_mode = mean_mode)
    else:
        return _bpm_3d_split(size, units,
                             lam = lam,
                             u0 = u0, dn = dn,
                             n_volumes = n_volumes,
                             subsample = subsample,
                             n0 = n0,                            
                             return_scattering = return_scattering,
                             return_g = return_g,
                             absorbing_width = absorbing_width,
                             return_full = return_full,
                             return_field = return_field,
                             use_fresnel_approx = use_fresnel_approx,
                             store_dn_as_half = store_dn_as_half,
                             mean_mode = mean_mode)


def _bpm_3d(size,
            units,
            lam = .5,
            u0 = None,
            dn = None,
            subsample = 1,
            n0 = 1.,
            return_scattering = False,
            return_g = False,
            return_full = True,
            return_field = True,
            use_fresnel_approx = False,
            absorbing_width = 0,
            scattering_plane_ind = 0,
            return_last_plane = False,
            store_dn_as_half = False,
            mean_mode = "weighted"):
    """
    simulates the propagation of monochromativ wave of wavelength lam with initial conditions u0 along z in a media filled with dn

    size     -    the dimension of the image to be calulcated  in pixels (Nx,Ny,Nz)
    units    -    the unit lengths of each dimensions in microns
    lam      -    the wavelength
    u0       -    the initial field distribution, if u0 = None an incident  plane wave is assumed
    dn       -    the refractive index of the medium (can be complex)
    mean_mode = "simple" or "weighted" or "ignore"
    """


    print mean_mode
    if not mean_mode in ["ignore","simple", "weighted"]:
        raise NotImplementedError("mean_mode  '%s' unknown"%mean_mode)

    if subsample != 1:
        raise NotImplementedError("subsample still has to be 1")

    clock = StopWatch()

    clock.tic("setup")

    Nx, Ny, Nz = size
    dx, dy, dz = units

    program = OCLProgram(absPath("kernels/bpm_3d_kernels.cl"))

    dn_sum_kernel= OCLReductionKernel(
        np.float32, neutral="0",
            reduce_expr="a+b",
            map_expr="cfloat_abs(field[i])*dn[i]",
            arguments="__global cfloat_t *field, __global float * dn")

    u_sum_kernel= OCLReductionKernel(
        np.float32, neutral="0",
            reduce_expr="a+b",
            map_expr="cfloat_abs(field[i])",
            arguments="__global cfloat_t *field")


    #setting up the propagator
    k0 = 2.*np.pi/lam

    kxs = 2.*np.pi*np.fft.fftfreq(Nx,dx)
    kys = 2.*np.pi*np.fft.fftfreq(Ny,dy)

    KY, KX = np.meshgrid(kys,kxs, indexing= "ij")

    #H0 = np.sqrt(0.j+n0**2*k0**2-KX**2-KY**2)
    H0 = np.sqrt(n0**2*k0**2-KX**2-KY**2)

    if use_fresnel_approx:
        H0  = 0.j+n0*k0-.5*(KX**2+KY**2)/n0/k0

    outsideInds = np.isnan(H0)

    H = np.exp(-1.j*dz*H0)

    H[outsideInds] = 0.
    H0[outsideInds] = 0.

    if u0 is None:
        u0 = np.ones((Ny,Nx),np.complex64)
    
    # setting up the gpu buffers and kernels

    plan = fft_plan((Ny,Nx))
    plane_g = OCLArray.from_array(u0.astype(np.complex64, copy = False))
    h_g = OCLArray.from_array(H.astype(np.complex64))

    program.run_kernel("compute_propagator",(Nx,Ny),None,
                       h_g.data,
                       np.float32(n0),np.float32(k0),
                       np.float32(dx),np.float32(dy),np.float32(dz)
                       )

    if dn is not None:
        if isinstance(dn,OCLArray):
            dn_g = dn
        else:
            if dn.dtype.type in (np.complex64,np.complex128):
                isComplexDn = True
                dn_g = OCLArray.from_array(dn.astype(np.complex64,copy= False))

            else:
                isComplexDn = False
                if store_dn_as_half:
                    dn_g = OCLArray.from_array(dn.astype(np.float16,copy= False))
                else:
                    dn_g = OCLArray.from_array(dn.astype(np.float32,copy= False))
    else:
        #dummy dn
        dn_g = OCLArray.empty((Nz,Ny,Nx),np.float32)

    if not dn is None:
        dn_mean = np.mean(dn,axis=(1,2))

    else:
        dn_mean = np.zeros(Nz, np.float32)
        #dn_mean = dn

    if mean_mode=="weighted":
        out_dn_sum = OCLArray.empty(1,np.float32)
        out_u_sum = OCLArray.empty(1,np.float32)

    if return_scattering:
        cos_theta = np.real(H0)/n0/k0

        # _H = np.sqrt(n0**2*k0**2-KX**2-KY**2)
        # _H[np.isnan(_H)] = 0.
        #
        # cos_theta = _H/n0/k0
        # # = cos(theta)
        scatter_weights = cos_theta

        #scatter_weights = np.sqrt(KX**2+KY**2)/k0/np.real(H0)
        #scatter_weights[outsideInds] = 0.

        scatter_weights_g = OCLArray.from_array(scatter_weights.astype(np.float32))

        # = cos(theta)^2
        gfactor_weights = cos_theta**2

        gfactor_weights_g = OCLArray.from_array(gfactor_weights.astype(np.float32))


        #return None,None,scatter_weights, gfactor_weights

        scatter_cross_sec_g = OCLArray.zeros(Nz,"float32")
        gfactor_g = OCLArray.zeros(Nz,"float32")

        plain_wave_dct = Nx*Ny*np.exp(-1.j*k0*n0*(scattering_plane_ind+np.arange(Nz))*dz).astype(np.complex64)


        reduce_kernel = OCLReductionKernel(
        np.float32, neutral="0",
            reduce_expr="a+b",
            map_expr="weights[i]*cfloat_abs(field[i]-(i==0)*plain)*cfloat_abs(field[i]-(i==0)*plain)",
            arguments="__global cfloat_t *field, __global float * weights,cfloat_t plain")

        # reduce_kernel = OCLReductionKernel(
        # np.float32, neutral="0",
        #     reduce_expr="a+b",
        #     map_expr = "weights[i]*(i!=0)*cfloat_abs(field[i])*cfloat_abs(field[i])",
        #     arguments = "__global cfloat_t *field, __global float * weights,cfloat_t plain")

    if return_full:
        if return_field:
            u_g = OCLArray.empty((Nz,Ny,Nx),dtype=np.complex64)
            u_g[0] = plane_g
        else:
            u_g = OCLArray.empty((Nz,Ny,Nx),dtype=np.float32)
            program.run_kernel("copy_intens",(Nx*Ny,),None,
                           plane_g.data,u_g.data, np.int32(0))


    clock.toc("setup")

    clock.tic("run")


    for i in range(Nz-1):
        fft(plane_g,inplace = True, plan  = plan)


        if mean_mode =="simple":
            dn0 = dn_mean[i+1]
            program.run_kernel("mult_propagator",(Nx,Ny),None,
                       plane_g.data,
                       np.float32(n0+dn0),np.float32(k0),
                       np.float32(dx),np.float32(dy),np.float32(dz)
                       )

        elif mean_mode =="weighted":
            # dn0 = float(dn_sum_kernel(plane_g,dn_g).get()/u_sum_kernel(plane_g).get())
            # program.run_kernel("mult_propagator",(Nx,Ny),None,
            #            plane_g.data,
            #            np.float32(n0+dn0),np.float32(k0),
            #            np.float32(dx),np.float32(dy),np.float32(dz)
            #            )
            dn_sum_kernel(plane_g,dn_g,out = out_dn_sum)
            u_sum_kernel(plane_g, out = out_u_sum)

            program.run_kernel("mult_propagator_buff",(Nx,Ny),None,
                       plane_g.data,
                               out_dn_sum.data,
                               out_u_sum.data,
                       np.float32(n0),np.float32(k0),
                       np.float32(dx),np.float32(dy),np.float32(dz)
                       )
        elif mean_mode =="ignore":
            dn0 = 0.
            program.run_kernel("mult",(Nx*Ny,),None,
                           plane_g.data,h_g.data)

        else:
            raise NotImplementedError



        #
        # program.run_kernel("compute_propagator",(Nx,Ny),None,
        #                h_g.data,
        #                np.float32(1.+dn_mean[i+1]),np.float32(k0),
        #                np.float32(dx),np.float32(dy),np.float32(dz)
        #                )
        # program.run_kernel("mult",(Nx*Ny,),None,
        #                    plane_g.data,h_g.data)





        if return_scattering:
            scatter_cross_sec_g[i+1] = reduce_kernel(plane_g,
                                                     scatter_weights_g,
                                                     plain_wave_dct[i+1])
            gfactor_g[i+1] = reduce_kernel(plane_g,
                                                     gfactor_weights_g,
                                                     plain_wave_dct[i+1])

        fft(plane_g,inplace = True, inverse = True,  plan  = plan)

        if dn is not None:
            if isComplexDn:

                kernel_str = "mult_dn_mean_complex"
            else:
                if dn_g.dtype.type == np.float16:
                    kernel_str = "mult_dn_mean_half"
                else:
                    kernel_str = "mult_dn_mean"

            if mean_mode =="weighted":
                kernel_str += "_buff"
                program.run_kernel(kernel_str,(Nx,Ny,),None,
                                   plane_g.data,dn_g.data,
                                   out_dn_sum.data,
                                   out_u_sum.data,
                                   np.float32(k0*dz),
                                   np.int32(Nx*Ny*(i+1)),
                               np.int32(absorbing_width))

            else:
                program.run_kernel(kernel_str,(Nx,Ny,),None,
                                   plane_g.data,dn_g.data,
                                   np.float32(k0*dz),
                                  np.float32(dn0),
                                   np.int32(Nx*Ny*(i+1)),
                               np.int32(absorbing_width))




        if return_full:
            if return_field:
                u_g[i+1] = plane_g
            else:
                program.run_kernel("copy_intens",(Nx*Ny,),None,
                           plane_g.data,u_g.data, np.int32(Nx*Ny*(i+1)))


    clock.toc("run")

    print clock

    if return_full:
        u = u_g.get()
    else:
        u = plane_g.get()
        if not return_field:
            u = np.abs(u)**2

    if return_scattering:
        # normalizing prefactor dkx = dx/Nx
        # prefac = 1./Nx/Ny*dx*dy/4./np.pi/n0
        prefac = 1./Nx/Ny*dx*dy
        p = prefac*scatter_cross_sec_g.get()


    if return_g:
        prefac = 1./Nx/Ny*dx*dy
        g = prefac*gfactor_g.get()/p



    if return_scattering:
        if return_g:
            result = u,  p, g
        else:
            result =  u,  p
    else:
        result = u

    if return_last_plane:
        if isinstance(result,tuple):
            result = result + (plane_g.get(),)
        else:
            result = (result, plane_g.get())


    return result



def _bpm_3d2(size,
            units,
            lam = .5,
            u0 = None,
            dn = None,
            subsample = 1,
            n0 = 1.,
            return_scattering = False,
            return_g = False,
            return_full = True,
            return_field = True,
            use_fresnel_approx = False,
            absorbing_width = 0,
            scattering_plane_ind = 0,
            return_last_plane = False,
            store_dn_as_half = False):
    """
    simulates the propagation of monochromatic wave of wavelength lam with initial conditions u0 along z in a media filled with dn

    size     -    the dimension of the image to be calulcated  in pixels (Nx,Ny,Nz)
    units    -    the unit lengths of each dimensions in microns
    lam      -    the wavelength
    u0       -    the initial field distribution, if u0 = None an incident  plane wave is assumed
    dn       -    the refractive index of the medium (can be complex)

    """


    if subsample != 1:
        raise NotImplementedError("subsample still has to be 1")

    clock = StopWatch()

    clock.tic("setup")

    Nx, Ny, Nz = size
    dx, dy, dz = units


    #setting up the propagator
    k0 = 2.*np.pi/lam

    kxs = 2.*np.pi*np.fft.fftfreq(Nx,dx)
    kys = 2.*np.pi*np.fft.fftfreq(Ny,dy)

    KY, KX = np.meshgrid(kys,kxs, indexing= "ij")

    #H0 = np.sqrt(0.j+n0**2*k0**2-KX**2-KY**2)
    H0 = np.sqrt(n0**2*k0**2-KX**2-KY**2)

    if use_fresnel_approx:
        H0  = 0.j+n0*k0-.5*(KX**2+KY**2)/n0/k0


    outsideInds = np.isnan(H0)

    H = np.exp(-1.j*dz*H0)

    H[outsideInds] = 0.
    H0[outsideInds] = 0.

    if u0 is None:
        u0 = np.ones((Ny,Nx),np.complex64)

    # setting up the gpu buffers and kernels

    program = OCLProgram(absPath("kernels/bpm_3d_kernels.cl"))

    plan = fft_plan((Ny,Nx))
    plane_g = OCLArray.from_array(u0.astype(np.complex64, copy = False))
    h_g = OCLArray.from_array(H.astype(np.complex64))

    if dn is not None:
        if isinstance(dn,OCLArray):
            dn_g = dn
        else:
            if dn.dtype.type in (np.complex64,np.complex128):
                isComplexDn = True
                dn_g = OCLArray.from_array(dn.astype(np.complex64,copy= False))

            else:
                isComplexDn = False
                if store_dn_as_half:
                    dn_g = OCLArray.from_array(dn.astype(np.float16,copy= False))
                else:
                    dn_g = OCLArray.from_array(dn.astype(np.float32,copy= False))

    else:
        #dummy dn
        dn_g = OCLArray.empty((1,)*3,np.float32)


    if return_scattering:
        cos_theta = np.real(H0)/n0/k0

        # _H = np.sqrt(n0**2*k0**2-KX**2-KY**2)
        # _H[np.isnan(_H)] = 0.
        #
        # cos_theta = _H/n0/k0
        # # = cos(theta)
        scatter_weights = cos_theta

        #scatter_weights = np.sqrt(KX**2+KY**2)/k0/np.real(H0)
        #scatter_weights[outsideInds] = 0.

        scatter_weights_g = OCLArray.from_array(scatter_weights.astype(np.float32))

        # = cos(theta)^2
        gfactor_weights = cos_theta**2

        gfactor_weights_g = OCLArray.from_array(gfactor_weights.astype(np.float32))


        #return None,None,scatter_weights, gfactor_weights

        scatter_cross_sec_g = OCLArray.zeros(Nz,"float32")
        gfactor_g = OCLArray.zeros(Nz,"float32")

        plain_wave_dct = Nx*Ny*np.exp(-1.j*k0*n0*(scattering_plane_ind+np.arange(Nz))*dz).astype(np.complex64)


        reduce_kernel = OCLReductionKernel(
        np.float32, neutral="0",
            reduce_expr="a+b",
            map_expr="weights[i]*cfloat_abs(field[i]-(i==0)*plain)*cfloat_abs(field[i]-(i==0)*plain)",
            arguments="__global cfloat_t *field, __global float * weights,cfloat_t plain")

        # reduce_kernel = OCLReductionKernel(
        # np.float32, neutral="0",
        #     reduce_expr="a+b",
        #     map_expr = "weights[i]*(i!=0)*cfloat_abs(field[i])*cfloat_abs(field[i])",
        #     arguments = "__global cfloat_t *field, __global float * weights,cfloat_t plain")

    if return_full:
        if return_field:
            u_g = OCLArray.empty((Nz,Ny,Nx),dtype=np.complex64)
            u_g[0] = plane_g
        else:
            u_g = OCLArray.empty((Nz,Ny,Nx),dtype=np.float32)
            program.run_kernel("copy_intens",(Nx*Ny,),None,
                           plane_g.data,u_g.data, np.int32(0))


    clock.toc("setup")

    clock.tic("run")


    for i in range(Nz-1):
        fft(plane_g,inplace = True, plan  = plan)

        program.run_kernel("mult",(Nx*Ny,),None,
                           plane_g.data,h_g.data)


        #a =  dn_g.sum()
        if return_scattering:
            scatter_cross_sec_g[i+1] = reduce_kernel(plane_g,
                                                     scatter_weights_g,
                                                     plain_wave_dct[i+1])
            gfactor_g[i+1] = reduce_kernel(plane_g,
                                                     gfactor_weights_g,
                                                     plain_wave_dct[i+1])

        fft(plane_g,inplace = True, inverse = True,  plan  = plan)

        if dn is not None:
            if isComplexDn:

                kernel_str = "mult_dn_complex"
            else:
                if dn_g.dtype.type == np.float16:
                    kernel_str = "mult_dn_half"
                else:
                    kernel_str = "mult_dn"


            program.run_kernel(kernel_str,(Nx,Ny,),None,
                                   plane_g.data,dn_g.data,
                                   np.float32(k0*dz),
                                   np.int32(Nx*Ny*(i+1)),
                               np.int32(absorbing_width))




        if return_full:
            if return_field:
                u_g[i+1] = plane_g
            else:
                program.run_kernel("copy_intens",(Nx*Ny,),None,
                           plane_g.data,u_g.data, np.int32(Nx*Ny*(i+1)))

    clock.toc("run")

    print clock

    if return_full:
        u = u_g.get()
    else:
        u = plane_g.get()
        if not return_field:
            u = np.abs(u)**2

    if return_scattering:
        # normalizing prefactor dkx = dx/Nx
        # prefac = 1./Nx/Ny*dx*dy/4./np.pi/n0
        prefac = 1./Nx/Ny*dx*dy
        p = prefac*scatter_cross_sec_g.get()


    if return_g:
        prefac = 1./Nx/Ny*dx*dy
        g = prefac*gfactor_g.get()/p



    if return_scattering:
        if return_g:
            result = u,  p, g
        else:
            result =  u,  p
    else:
        result = u

    if return_last_plane:
        if isinstance(result,tuple):
            result = result + (plane_g.get(),)
        else:
            result = (result, plane_g.get())


    return result



def _bpm_3d_image(size,
            units,
            lam = .5,
            u0 = None, dn = None,
            subsample = 1,
            n0 = 1.,
            return_scattering = False,
            return_g = False,
            return_full_last = False,
            use_fresnel_approx = False,
            ):
    """
    simulates the propagation of monochromativ wave of wavelength lam with initial conditions u0 along z in a media filled with dn

    size     -    the dimension of the image to be calulcated  in pixels (Nx,Ny,Nz)
    units    -    the unit lengths of each dimensions in microns
    lam      -    the wavelength
    u0       -    the initial field distribution, if u0 = None an incident  plane wave is assumed
    dn       -    the refractive index of the medium (can be complex)

    """
    clock = StopWatch()

    clock.tic("setup")

    Nx, Ny, Nz = size
    dx, dy, dz = units

    # subsampling
    Nx2, Ny2, Nz2 = (subsample*N for N in size)
    dx2, dy2, dz2 = (1.*d/subsample for d in units)

    #setting up the propagator
    k0 = 2.*np.pi/lam

    kxs = 2.*np.pi*np.fft.fftfreq(Nx2,dx2)
    kys = 2.*np.pi*np.fft.fftfreq(Ny2,dy2)

    KY, KX = np.meshgrid(kys,kxs, indexing= "ij")

    #H0 = np.sqrt(0.j+n0**2*k0**2-KX**2-KY**2)
    H0 = np.sqrt(n0**2*k0**2-KX**2-KY**2)

    if use_fresnel_approx:
        H0  = 0.j+n0**2*k0-.5*(KX**2+KY**2)


    outsideInds = np.isnan(H0)

    H = np.exp(-1.j*dz2*H0)

    H[outsideInds] = 0.
    H0[outsideInds] = 0.

    if u0 is None:
        u0 = np.ones((Ny2,Nx2),np.complex64)
    else:
        if subsample >1:
            u0 = zoom(np.real(u0),subsample) + 1.j*zoom(np.imag(u0),subsample)

    # setting up the gpu buffers and kernels

    program = OCLProgram(absPath("kernels/bpm_3d_kernels.cl"))

    plan = fft_plan((Ny2,Nx2))
    plane_g = OCLArray.from_array(u0.astype(np.complex64))

    h_g = OCLArray.from_array(H.astype(np.complex64))

    if dn is not None:
        if isinstance(dn,OCLImage):
            dn_g = dn
        else:
            if dn.dtype.type in (np.complex64,np.complex128):

                dn_complex = np.zeros(dn.shape+(2,),np.float32)
                dn_complex[...,0] = np.real(dn)
                dn_complex[...,1] = np.imag(dn)
                dn_g = OCLImage.from_array(dn_complex)

            else:
                dn_g = OCLImage.from_array(dn.astype(np.float32))

        isComplexDn = dn.dtype.type in (np.complex64,np.complex128)

    else:
        #dummy dn
        dn_g = OCLArray.empty((1,)*3,np.float16)


    if return_scattering:
        cos_theta = np.real(H0)/n0/k0

        # = cos(theta)
        scatter_weights = cos_theta

        scatter_weights_g = OCLArray.from_array(scatter_weights.astype(np.float32))

        # = cos(theta)^2
        gfactor_weights = cos_theta**2

        gfactor_weights_g = OCLArray.from_array(gfactor_weights.astype(np.float32))


        #return None,None,scatter_weights, gfactor_weights

        scatter_cross_sec_g = OCLArray.zeros(Nz,"float32")
        gfactor_g = OCLArray.zeros(Nz,"float32")

        plain_wave_dct = Nx2*Ny2*np.exp(-1.j*k0*n0*np.arange(Nz)*dz).astype(np.complex64)


        reduce_kernel = OCLReductionKernel(
        np.float32, neutral="0",
            reduce_expr="a+b",
            map_expr="weights[i]*cfloat_abs(field[i]-(i==0)*plain)*cfloat_abs(field[i]-(i==0)*plain)",
            arguments="__global cfloat_t *field, __global float * weights,cfloat_t plain")

        # reduce_kernel = OCLReductionKernel(
        # np.float32, neutral="0",
        #     reduce_expr="a+b",
        #     map_expr = "weights[i]*(i!=0)*cfloat_abs(field[i])*cfloat_abs(field[i])",
        #     arguments = "__global cfloat_t *field, __global float * weights,cfloat_t plain")


    u_g = OCLArray.empty((Nz,Ny,Nx),dtype=np.complex64)

    program.run_kernel("copy_subsampled_buffer",(Nx,Ny),None,
                           u_g.data,plane_g.data,
                           np.int32(subsample),
                           np.int32(0))


    clock.toc("setup")

    clock.tic("run")

    for i in range(Nz-1):
        for substep in range(subsample):
            fft(plane_g,inplace = True, plan  = plan)

            program.run_kernel("mult",(Nx2*Ny2,),None,
                               plane_g.data,h_g.data)

            if return_scattering and substep == (subsample-1):
                scatter_cross_sec_g[i+1] = reduce_kernel(plane_g,
                                                     scatter_weights_g,
                                                     plain_wave_dct[i+1])
                gfactor_g[i+1] = reduce_kernel(plane_g,
                                                     gfactor_weights_g,
                                                     plain_wave_dct[i+1])

            fft(plane_g,inplace = True, inverse = True,  plan  = plan)

            if dn is not None:
                if isComplexDn:

                    program.run_kernel("mult_dn_complex_image",(Nx2,Ny2),None,
                                   plane_g.data,dn_g,
                                   np.float32(k0*dz2),
                                   np.float32(n0),
                                   np.int32(subsample*(i+1.)+substep),
                                   np.int32(subsample))
                else:
                    program.run_kernel("mult_dn_image",(Nx2,Ny2),None,
                                   plane_g.data,dn_g,
                                   np.float32(k0*dz2),
                                   np.float32(n0),
                                   np.int32(subsample*(i+1.)+substep),
                                   np.int32(subsample))


        program.run_kernel("copy_subsampled_buffer",(Nx,Ny),None,
                           u_g.data,plane_g.data,
                           np.int32(subsample),
                           np.int32((i+1)*Nx*Ny))


    clock.toc("run")

    print clock
    result = (u_g.get(), dn_g.get(),)

    if return_scattering:
        # normalizing prefactor dkx = dx2/Nx2
        # prefac = 1./Nx2/Ny2*dx2*dy2/4./np.pi/n0
        prefac = 1./Nx2/Ny2*dx2*dy2
        p = prefac*scatter_cross_sec_g.get()
        result += (p,)

    if return_g:
        prefac = 1./Nx2/Ny2*dx2*dy2
        g = prefac*gfactor_g.get()/p
        result += (g,)

    if return_full_last:
        result += (plane_g.get(),)

    return result

def _bpm_3d_split(size, units, lam = .5, u0 = None, dn = None,
                 n_volumes = 1,
                 n0 = 1.,
                 subsample=1,
                 return_scattering = False,
                  return_full = True,
                 return_g = False,
                  return_field = True,
                  absorbing_width = 0,
                 use_fresnel_approx = False,
                  store_dn_as_half = False,
                  mean_mode = "weighted"):
    """
    same as bpm_3d but splits z into n_volumes pieces (e.g. if memory of GPU is not enough)
    """
    
    Nx, Ny, Nz = size

    Nz2 = Nz/n_volumes+1

    if u0 is None:
        u0 = np.ones((subsample*Ny,subsample*Nx),np.complex64)


    if return_field:
        u_dtype = np.complex64
    else:
        u_dtype = np.float32

    if return_full:
        u = np.empty((Nz,Ny,Nx),u_dtype)
        u_part = np.empty((Nz2,Ny,Nx),u_dtype)
    else:
        u = np.empty((Ny,Nx),u_dtype)
        u_part = np.empty((Ny,Nx),u_dtype)

    p = np.empty(Nz,np.float32)
    g = np.empty(Nz,np.float32)

    for i in range(n_volumes):
        i1,i2 = i*Nz2, np.clip((i+1)*Nz2,0,Nz)
        if i<n_volumes-1:
            if dn is None:
                dn_part = None
            else:
                dn_part = dn[i1:i2+1,:,:]

            res_part = _bpm_3d((Nx,Ny,i2-i1+1),
                               units = units,
                               lam = lam,
                               u0 = u0,
                               dn = dn_part,
                               n0 = n0,
                               subsample = subsample,
                               return_full = return_full,
                               return_field = return_field,
                               return_last_plane=True,
                               return_g = return_g,
                               absorbing_width = absorbing_width,
                               return_scattering = return_scattering,
                               scattering_plane_ind = Nz2*i,
                               store_dn_as_half = store_dn_as_half,
                               mean_mode = mean_mode)


            if return_scattering:
                if return_g:
                    u_part, p_part, g_part, u0= res_part
                    g[i1:i2] = g_part[1:]
                else:
                    u_part, p_part, u0= res_part
                p[i1:i2] = p_part[1:]
            else:
                u_part, u0 = res_part

            if return_full:
                u[i1:i2,...] = u_part[:-1,...]
                #u0 = u_part[-1]
            else:
                pass
                #u0 = u_part

        else:
            if dn is None:
                dn_part = None
            else:
                dn_part = dn[i1:i2,:,:]

            res_part = _bpm_3d((Nx,Ny,i2-i1),
                               units = units,
                               lam = lam,
                               u0 = u0,
                               dn = dn_part,
                               n0 = n0,
                               subsample = subsample,
                               return_full = return_full,
                               return_field = return_field,
                               return_g = return_g,
                               absorbing_width = absorbing_width,
                               return_scattering = return_scattering,
                               scattering_plane_ind = Nz2*i,
                               store_dn_as_half = store_dn_as_half,
                               mean_mode = mean_mode)

            if return_scattering:
                if return_g:
                    u_part, p_part, g_part = res_part
                    g[i1:i2] = g_part
                    g[i1] = g[i1-1]
                else:
                    u_part, p_part = res_part
                p[i1:i2] = p_part
                p[i1] = p[i1-1]
            else:

                u_part = res_part
            if return_full:
                u[i1:i2,...] = u_part
                #u0 = u_part[-1]
            else:
                u = u_part


    if return_scattering:
        if return_g:
            return u,  p, g
        else:
            return u,  p
    else:
        return u
    
def bpm_3d_free(size, units, dz, lam = .5, u0 = None,
                n0 = 1., 
                use_fresnel_approx = False):
    """propagates the field u0 to distance dz
    """
    clock = StopWatch()

    clock.tic("setup")
    Nx, Ny = size
    dx, dy = units

    #setting up the propagator
    k0 = 2.*np.pi/lam*n0

    kxs = np.arange(-Nx/2.,Nx/2.)/Nx
    kys = np.arange(-Ny/2.,Ny/2.)/Ny

    KY, KX = np.meshgrid(kxs,kys, indexing= "ij")

    H0 = np.sqrt(0.j+(1./lam)**2-KX**2/dx**2-KY**2/dy**2)

    if use_fresnel_approx:
        H0  = 1./lam*(0.j+1.-.5*lam**2*(KX**2/dx**2+KY**2/dy**2))

        
    outsideInds = np.isnan(H0)
    H = np.exp(2.j*np.pi*dz*H0)
    H[outsideInds] = 0.
    H0[outsideInds] = 0.

    H = np.fft.fftshift(H).astype(np.complex64)


    if u0 is None:
        u0 = np.ones((Ny,Nx),np.complex64)

    """
    setting up the gpu buffers and kernels
    """

    program = OCLProgram(absPath("kernels/bpm_3d_kernels.cl"))
    # program = OCLProgram(src_str = kernel_str)

    plan = ocl_fft_plan((Ny,Nx))
    plane_g = OCLArray.from_array(u0.astype(np.complex64))

    h_g = OCLArray.from_array(H.astype(np.complex64))

 
    clock.toc("setup")
    clock.tic("run")

    fft(plane_g,inplace = True, plan  = plan)

    program.run_kernel("mult",(Nx*Ny,),None,
                           plane_g.data,h_g.data)

    fft(plane_g,inplace = True, inverse = True,  plan  = plan)

    clock.toc("run")

    return plane_g.get()
    


def test_speed():
    from time import time
    
    for N in [64,128,256,512,1024][2:]:
        shape = (N,N,64)
        bpm_3d(shape,units= (.1,)*3)

        Niter = 3
        t = time()
        for i in range(Niter):
            bpm_3d(shape,units= (.1,)*3)
        
        print "time to bpm through %s = %.3f ms"%(shape,1000.*(time()-t)/Niter)

def test_plane(n_x_comp = 0, n0 = 1.):
    """ propagates a plane wave freely
    n_x_comp is the tilt in x
    """
    Nx, Nz = 128,128
    dx, dz = .05, 0.05

    lam = .5

    units = (dx,dx,dz)
    
    x = dx*np.arange(Nx)
    y = dx*np.arange(Nx)
    z = dz*np.arange(Nz)
    Z,Y,X = np.meshgrid(z,y,x,indexing="ij")

    
    k_x = 1.*n_x_comp/(dx*(Nx-1.))

    
    k_z = np.sqrt(1.*n0**2/lam**2-k_x**2)

    print np.sqrt(k_x**2+k_z**2)
    
    u_plane = np.exp(2.j*np.pi*(k_z*Z+k_x*X))

    u = 0

    u, dn = bpm_3d((Nx,Nx,Nz),units= units, lam = lam,
                   n0 = n0,
                   subsample = 2,
                   u0 = u_plane[0,...])

    # u, dn = bpm_3d_old((Nx,Nx,Nz),units= units, lam = lam,
    #                u0 = u_plane[0,...])
    
    print np.mean(np.abs(u_plane-u)**2)
    return u, u_plane

def test_slit():
    Nx, Nz = 128,128
    dx, dz = .05, 0.05

    lam = 0.5

    units = (dx,dx,dz)

    
    
    x = np.linspace(-1,1,Nx)
    y = np.linspace(-1,1,Nx)
    Y,X = np.meshgrid(y,x,indexing="ij")

    R = np.hypot(Y,X)

    u0 = 1.*(R<.5) 

    
    u, dn, p = bpm_3d((Nx,Nx,Nz),units= units,
                      lam = lam,
                      u0 = u0,
                      dn = np.zeros((Nz,Nx,Nx)),
                      subsample = 1,
                      return_scattering = True )

    return u, dn, p


def test_sphere(N = 128, **kwargs):
    Nx, Nz = N,N
    dx, dz = .05, 0.05

    lam = .5

    units = (dx,dx,dz)
    
    x = Nx/2*dx*np.linspace(-1,1,Nx)
    y = Nx/2*dx*np.linspace(-1,1,Nx)
    
    x = dx*np.arange(-Nx/2,Nx/2)
    y = dx*np.arange(-Nx/2,Nx/2)
    z = dz*np.arange(0,Nz)
    Z,Y,X = np.meshgrid(z,y,x,indexing="ij")
    R = np.sqrt(X**2+Y**2+(Z-3.)**2)
    dn = .05*(R<1.)

    c = StopWatch()
    c.tic("bpm")

    u = bpm_3d((Nx,Nx,Nz),units= units,
                      lam = lam,
                      dn = dn,
                    **kwargs
               )

    c.toc("bpm")
    print c

    return u

def test_compare():
    Nx, Nz = 128,256
    dx, dz = .05, 0.05

    lam = .5

    units = (dx,dx,dz)

    
    
    x = Nx/2*dx*np.linspace(-1,1,Nx)
    y = Nx/2*dx*np.linspace(-1,1,Nx)
    
    x = dx*np.arange(-Nx/2,Nx/2)
    y = dx*np.arange(-Nx/2,Nx/2)
    z = dz*np.arange(0,Nz)
    Z,Y,X = np.meshgrid(z,y,x,indexing="ij")
    R = np.sqrt(X**2+Y**2+(Z-3.)**2)
    dn = .05*(R<1.)
    
    u1, dn1, p1 = bpm_3d((Nx,Nx,Nz),units= units,
                      lam = lam,
                      dn = dn,
                      subsample = 1,
                      n_volumes = 1,
                      return_scattering = True )

    u2, dn2, p2 = bpm_3d_old((Nx,Nx,Nz),units= units,
                      lam = lam,
                      dn = dn,
                      return_scattering = True )
    
    return u1, u2


if __name__ == '__main__':
    # test_speed()

    #u = test_sphere()

    N = 256
    dx = .05
    from bpm import psf_u0

    if not "dn" in locals():
        dn = np.zeros((N,)*3,np.float32)
        dn[:40,...] += .2
        u0 = psf_u0((N,)*2,(dx,)*2,zfoc = N/2.*dx,NA = .4)

    u1 = bpm_3d(dn.shape[::-1],(dx,)*3, u0=u0,dn = dn, n_volumes=1)
    #u2 = _bpm_3d2(dn.shape[::-1],(dx,)*3, u0=u0,dn = dn)

    

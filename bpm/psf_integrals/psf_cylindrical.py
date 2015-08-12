"""
PSF calulcations for cylidrical lenses

see e.g.
Purnapatra, Subhajit B. Mondal, Partha P.
Determination of electric field at and near the focus of a cylindrical lens for applications in fluorescence microscopy (2013)
"""


from volust.volgpu import *
from volust.volgpu.oclalgos import *

import numpy as np

import time


import os
import sys

def absPath(myPath):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
        return os.path.join(base_path, os.path.basename(myPath))
    except Exception:
        base_path = os.path.abspath(os.path.dirname(__file__))
        return os.path.join(base_path, myPath)



def psf_cylindrical(shape,units,lam,NA, n_integration_steps = 100):
    """returns psf of cylindrical lerns with given NA
    """

    p = OCLProgram(absPath("psf_cylindrical.cl"),build_options = str("-I %s -D INT_STEPS=%s"%(absPath("."),n_integration_steps)))

    
    Nx, Ny, Nz = shape
    dx, dy, dz = units

    alpha = np.arcsin(NA)
    
    u_g = OCLArray.empty((Nz,Ny),np.float32)
    ex_g = OCLArray.empty((Nz,Ny),np.complex64)

    t = time.time()
    
    p.run_kernel("psf_cylindrical",u_g.shape[::-1],None,
                 ex_g.data,u_g.data,
                 np.float32(-dy*(Ny-1)/2.),np.float32(dy*(Ny-1)/2.),
                 np.float32(-dz*(Nz-1)/2.),np.float32(dz*(Nz-1)/2.),
                 np.float32(lam),np.float32(alpha))

    u = np.array(np.repeat(u_g.get()[...,np.newaxis],Nx,axis=-1))
    ex = np.array(np.repeat(ex_g.get()[...,np.newaxis],Nx,axis=-1))

    
    print "time in secs:" , time.time()-t
    

    return u,ex


def psf_cylindrical_focus_u0(shape,units,zfoc,lam,NA, n_integration_steps = 200):
    """calculates initial plane u0 of a cylidrical lense beam focused at zfoc
    shape = (Nx,Ny)
    units = (dx,dy)
    NA = e.g. 0.6
    """

    Nx, Ny = shape
    dx, dy = units

    
    h , ex = psf_cylindrical((Nx,Ny,2),(dx,dy,4*zfoc),
                              lam = lam,NA = NA)

    return ex
    return ex[0,...]

def test_cylinder():
    
    lam = .5

    Nx = 128
    Ny = 128
    Nz = 128
    
    dx = .1
    dy = .1
    dz = .1

    NA = .2
    
    u, ex = psf_cylindrical((Nx,Ny,Nz),(dx,dy,dz),
                     lam = lam, NA = NA, n_integration_steps= 100)

    u0 = psf_cylindrical_focus_u0((Nx,Ny),(dx,dy),Nz*dz/2.,lam,NA)

    return u,u0

if __name__ == '__main__':

    u, u0 = test_cylinder() 

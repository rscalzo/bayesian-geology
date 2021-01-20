#!/usr/bin/env python

"""
RS 2020/10/09:  MCMC Demo on Kinematic Models

This separates the MCMC dependencies out from the main code.  It's basically
the same infrastructure I've been using in the notebooks, put into production.
"""

import numpy as np
import matplotlib.pyplot as plt
import scipy.special as sp
from blockworlds import profile_timer, DiscreteGravity
from blockworlds import baseline_tensor_mesh, survey_gridded_locations
from blockworlds import gen_two_fault_model_demo, gen_fold_model_demo
from riemann import Sampler, Model
from riemann.proposals.randomwalk import AdaptiveMetropolisRandomWalk as AMRW
from riemann.samplers.ptsampler import PTSampler
import pickle
import emcee


# Reproducible random numbers
np.random.seed(413)

# True values of parameters for various demo models
fault_model_pars = np.array([
    # Original graben model
    [3.0, 350.0, 2.5, 190.0, 2.0,           # layer densities and thicknesses
     -400.0, 0.0, +20.0, 0.0, -220.0,       # 1st fault x0, y0, theta, phi, s
     +400.0, 0.0, -20.0, 0.0, +220.0],      # 2nd fault x0, y0, theta, phi, s
    # Mark Lindsay's implicit model 1
    [3.0, 350.0, 2.5, 190.0, 2.0,
     -400.0, 0.0, +45.0, 0.0, -220.0,
     +400.0, 0.0, -45.0, 0.0, +220.0],
    # Mark Lindsay's implicit model 2
    [3.0, 350.0, 2.5, 190.0, 2.0,
     -450.0, 0.0, 45.0, 0.0, -220.0,
      +50.0, 0.0, 20.0, 0.0, +220.0],
    # Mark Lindsay's implicit model 3
    [3.0, 350.0, 2.5, 190.0, 2.0,
     -50.0, 0.0, -20.0, 0.0, -220.0,
     +50.0, 0.0, 20.0, 0.0, +220.0],
    # Mark Lindsay's implicit model 4
    [3.0, 350.0, 2.5, 190.0, 2.0,
     -250.0, 0.0, 0.0, 0.0, -220.0,
     +250.0, 0.0, 0.0, 0.0, +220.0],
    # Mark Lindsay's implicit model 5
    [3.0, 350.0, 2.5, 190.0, 2.0,
     -450.0, 0.0, 30.0, 0.0, 220.0,
     +50.0, 0.0, 10.0, 0.0, -220.0],
    # Mark Lindsay's implicit model 6
    [3.0, 350.0, 2.5, 190.0, 2.0,
     -400.0, 0.0, 20.0, 0.0, 220.0,
     -300.0, 0.0, 40.0, 0.0, -220.0],
    # Mark Lindsay's implicit model 7
    [3.0, 350.0, 2.5, 190.0, 2.0,
     -400.0, 0.0, 20.0, 0.0, 140.0,
     -300.0, 0.0, 40.0, 0.0, 80.0],
])

# Labels for plots
fault_model_parnames = [
    "Basement Density (g cm$^{-3}$)",
    "Layer 1 Thickness (m)",
    "Layer 1 Density (g cm$^{-3}$)",
    "Layer 2 Thickness (m)",
    "Layer 2 Density (g cm$^{-3}$)",
    "Fault 1 Contact X Position (m)",
    "Fault 1 Contact Y Position (m)",
    "Fault 1 Polar Elevation (deg)",
    "Fault 1 Polar Azimuth (deg)",
    "Fault 1 Dip-Direction Slip (m)",
    "Fault 2 Contact X Position (m)",
    "Fault 2 Contact Y Position (m)",
    "Fault 2 Polar Elevation (deg)",
    "Fault 2 Polar Azimuth (deg)",
    "Fault 2 Dip-Direction Slip (m)",
]

# Characteristic initial-guess step sizes for MCMC
stepsizes = np.array([0.1, 10.0, 0.02, 10.0, 0.02,
                      0.2, 0.2, 3.0, 3.0, 30.0,
                      0.2, 0.2, 3.0, 3.0, 30.0])

# Mark Lindsay's fold model
fold_model_pars = np.array([
    [3.0, 350.0, 2.5, 190.0, 2.0,
     0.0, 0.0, 0.0, 0.0, 1000.0, 100.0,
     250.0, 0.0, -205.0, 0.0, 200.0]
])

fold_model_parnames = [
    "Basement Density (g cm$^{-3}$)",
    "Layer 1 Thickness (m)",
    "Layer 1 Density (g cm$^{-3}$)",
    "Layer 2 Thickness (m)",
    "Layer 2 Density (g cm$^{-3}$)",
    "Fold 1 Axis Elevation (deg)",
    "Fold 1 Axis Azimuth (deg)",
    "Fold 1 Pitch Angle (deg)",
    "Fold 1 Phase Angle (deg)",
    "Fold 1 Wavelength (m)",
    "Fold 1 Amplitude (m)",
    "Fault 1 Contact X Position (m)",
    "Fault 1 Contact Y Position (m)",
    "Fault 1 Polar Elevation (deg)",
    "Fault 1 Polar Azimuth (deg)",
    "Fault 1 Dip-Direction Slip (m)",
]


# ============================================================================
#             riemann classes specifying statistical model for MCMC
# ============================================================================


class GeoModel(Model):

    def __init__(self, history, fwdmodel, dsynth, sigdata, alpha=1e+4):
        # Set baseline attributes
        self.history = history
        self.dsynth = dsynth
        self.sigdata = sigdata
        self.alpha = alpha
        # Pre-compute constant norm for t-distribution likelihood
        self.beta = sigdata**2 * alpha
        self.tnorm = (sp.gammaln(self.alpha + 0.5) - sp.gammaln(self.alpha)
                      - 0.5*np.log(2*np.pi) + self.alpha*np.log(self.beta))
        # Set forward model to represent history
        self.set_fwdmodel(fwdmodel)
        # Turn full anti-aliasing on; match h to grid resolution
        self.set_antialiasing(True)

    def set_fwdmodel(self, fwdmodel):
        self.fwdmodel = fwdmodel
        self.fwdmodel.gfunc = self.history.rockprops
        mesh = self.fwdmodel.mesh
        self.hmesh = np.exp(np.mean(np.log([mesh.hx, mesh.hy, mesh.hz])))

    def set_antialiasing(self, antialiasing):
        if antialiasing:
            self.h = self.hmesh
        else:
            self.h = 0.001*self.hmesh

    def log_likelihood(self, theta):
        # Load this parameter vector into the history and calculate gravity
        self.history.deserialize(theta)
        dpred = self.fwdmodel.calc_gravity(self.h)
        resids = dpred - self.dsynth
        resids = resids - resids.mean()
        # if self.alpha > 1000:
        if self.alpha > 100:
            # Independent Gaussian likelihood with variance sigdata**2
            return -0.5 * np.sum(resids ** 2 / self.sigdata ** 2 +
                                 np.log(2 * np.pi * self.sigdata ** 2))
        else:
            # Independent t-distribution likelihood with tail weight alpha
            # and scale parameter sigdata = np.sqrt(beta/alpha)
            A, B, T = self.alpha, self.beta, self.tnorm
            return np.sum(-(A + 0.5) * np.log(B + 0.5 * resids**2) + T)


    def log_prior(self, theta):
        # Load this parameter vector into the history and calculate prior density
        self.history.deserialize(theta)
        return self.history.logprior()


def initialize_geomodel(pars, L, NL, foldhistory=False):
    """
    Initialize a GeoModel instance and get it ready for sampling
    :param pars: parameter vector (see top of file)
    :return: GeoModel instance ready for sampling with riemann
    """
    # Initialize a mesh for forward gravity calculation
    z0, h = 0.0, L / NL
    print("z0, L, nL, h =", z0, L, NL, h)
    mesh = baseline_tensor_mesh(NL, h, centering='CCN')
    survey = survey_gridded_locations(L, L, 20, 20, z0)

    # Initialize a GeoHistory based on the parameters passed in
    history = gen_two_fault_model_demo(pars)
    history.deserialize(pars)

    # Initialize a DiscreteGravity forward model instance
    fwdmodel = DiscreteGravity(mesh, survey, history.rockprops)

    # Make some synthetic data based on this history and mesh
    data0 = fwdmodel.calc_gravity(h)
    np.random.seed(413)
    sigdata = 0.05 * np.std(data0)
    epsilon = sigdata * np.random.normal(size=data0.shape)
    dsynth = data0 + epsilon

    # Construct and return a GeoModel
    return GeoModel(history, fwdmodel, dsynth, sigdata, alpha=2.5)

def run_mcmc(model, Nsamp=100000, Nburn=20000, Nthin=100, temper=False):
    """
    Runs the MCMC using riemann.
    :param model: GeoModel instance
    :return: chain
    """
    print("run_mcmc: running chain...")
    model.history.set_to_prior_draw()
    histpars = model.history.serialize()
    proposal = AMRW(0.1 * np.diag(stepsizes), 100, marginalize=False)
    if temper:
        sampler = PTSampler(model, proposal, np.array(histpars),
                            betas=0.5**np.arange(10))
    else:
        sampler = Sampler(model, proposal, np.array(histpars))
    profile_timer(sampler.run, Nsamp)
    chain = np.array(sampler._chain_thetas)
    accept_frac = np.mean(chain[1:] - chain[:-1] != 0)
    tau = emcee.autocorr.integrated_time(chain, quiet=True)
    print("run_mcmc: chain finished; acceptance fraction =", accept_frac)
    print("run_mcmc:  autocorrelation time = ", tau)
    return chain[Nburn:Nsamp:Nthin]

def gelman_rubin(data, verbose=False):
    """
    Apply Gelman-Rubin convergence diagnostic to a collection of chains.
    :param data: np.array of shape (Nchains, Nsamples, Npars)
    """
    Nchains, Nsamples, Npars = data.shape
    B_on_n = data.mean(axis=1).var(axis=0)      # variance of in-chain means
    W = data.var(axis=1).mean(axis=0)           # mean of in-chain variances

    # simple version, as in Obsidian
    sig2 = (Nsamples/(Nsamples-1))*W + B_on_n
    Vhat = sig2 + B_on_n/Nchains
    Rhat = Vhat/W

    # advanced version that accounts for degrees of freedom
    # see Gelman & Rubin, Statistical Science 7:4, 457-472 (1992)
    m, n = np.float(Nchains), np.float(Nsamples)
    si2 = data.var(axis=1)
    xi_bar = data.mean(axis=1)
    xi2_bar = data.mean(axis=1)**2
    var_si2 = data.var(axis=1).var(axis=0)
    allmean = data.mean(axis=1).mean(axis=0)
    cov_term1 = np.array([np.cov(si2[:,i], xi2_bar[:,i])[0,1]
                          for i in range(Npars)])
    cov_term2 = np.array([-2*allmean[i]*(np.cov(si2[:,i], xi_bar[:,i])[0,1])
                          for i in range(Npars)])
    var_Vhat = ( ((n-1)/n)**2 * 1.0/m * var_si2
             +   ((m+1)/m)**2 * 2.0/(m-1) * B_on_n**2
             +   2.0*(m+1)*(n-1)/(m*n**2)
                    * n/m * (cov_term1 + cov_term2))
    df = 2*Vhat**2 / var_Vhat
    if verbose:
        print("gelman_rubin(): var_Vhat = {}".format(var_Vhat))
        print("gelman_rubin(): df = {}".format(df))
    Rhat *= df/(df-2)

    return Rhat


# ============================================================================
#                    Helper functions for figure generation
# ============================================================================


def run_grid(my_model, p1_vals, p2_vals, p1_idx, p2_idx):
    origpars = my_model.history.serialize()
    grid_vals = [ ]
    for p1i in p1_vals:
        for p2i in p2_vals:
            theta = np.array(origpars)
            theta[p1_idx], theta[p2_idx] = p1i, p2i
            grid_vals.append([p1i, p2i, my_model.log_posterior(theta)])
    my_model.history.deserialize(origpars)
    grid_vals = np.array(grid_vals).T.reshape(3, len(p1_vals), len(p2_vals))
    return grid_vals

def show_contours(xg, yg, Lg, p1_vals, p2_vals, p1_0, p2_0):
    levels = 10**np.arange(-6,0.1)
    levels = np.arange(-6,0.1)
    # plt.contourf(xg, yg, np.exp(Lg - Lg.max()), levels=levels)
    plt.contourf(xg, yg, (Lg - Lg.max())/np.log(10), levels=levels)
    plt.colorbar()
    ax = plt.gca()
    ax.set_xlim(p1_vals.min(), p1_vals.max())
    ax.set_ylim(p2_vals.min(), p2_vals.max())
    ax.set_title("$\log_{10} P(\\theta|d)/P(\\theta_\mathrm{MAP}|d)$")

def vet_slice(my_model, z1_idx, z2_idx, zdelt1, zdelt2, Nz):
    histpars = np.array(my_model.history.serialize())
    z1_0, z2_0 = histpars[z1_idx], histpars[z2_idx]
    z1_vals = np.linspace(z1_0-0.5*zdelt1, z1_0+0.5*zdelt1, Nz)
    z2_vals = np.linspace(z2_0-0.5*zdelt2, z2_0+0.5*zdelt2, Nz)
    xg, yg, Lg = profile_timer(run_grid, my_model, z1_vals, z2_vals, z1_idx, z2_idx)
    show_contours(xg, yg, Lg, z1_vals, z2_vals, z1_0, z2_0)
    return xg, yg, Lg


# ============================================================================
#                               Main routine(s)
# ============================================================================


def run_fault_model_sampling():
    L, NL = 1000.0, 15
    M, Nsamp, Nburn, Nthin, temper = 4, 100000, 20000, 10, False
    model_chains_h0, model_chains_h1 = [ ], [ ]
    estimated_model_runtime = M * Nsamp * 360/1e+5
    if temper:
        estimated_model_runtime *= 10
    estimated_total_runtime = 2 * len(fault_model_pars) * estimated_model_runtime
    print("starting experimental run")
    print("estimated model run time: {:.2} s".format(estimated_model_runtime))
    print("estimated total run time: {:.2} s".format(estimated_total_runtime))
    for i, pars in enumerate(fault_model_pars):
        # Initialize model and grab the mesh cell size
        model = initialize_geomodel(pars, L, NL)
        basefname = "implicit_{}_chains".format(i)
        # Sample first with anti-aliasing on
        print("anti-aliasing ON:  model.h =", model.h)
        chains = np.array([run_mcmc(model, Nsamp, Nburn, Nthin, temper=temper)
                           for i in range(M)])
        model_chains_h1.append(np.array(chains))
        print("chains_h1.shape =", chains.shape)
        print("gelman_rubin(): Rhat = {}".format(gelman_rubin(chains)))
        with open(basefname + "_h1.pkl", 'wb') as pklfile:
            pickle.dump(np.array(chains), pklfile)
        # Turn anti-aliasing back on and sample again
        model.h *= 0.001
        print("anti-aliasing OFF:  model.h =", model.h)
        chains = np.array([run_mcmc(model, Nsamp, Nburn, Nthin, temper=temper)
                           for i in range(M)])
        model_chains_h0.append(np.array(chains))
        print("chains_h0.shape =", chains.shape)
        print("gelman_rubin(): Rhat = {}".format(gelman_rubin(chains)))
        # Save to pickles for later; most likely faster than re-running!
        with open(basefname + "_h0.pkl", 'wb') as pklfile:
            pickle.dump(np.array(chains), pklfile)

    with open("model_chains_h1.pkl", 'wb') as pklfile:
        pickle.dump(np.array(model_chains_h1), pklfile)
    with open("model_chains_h0.pkl", 'wb') as pklfile:
        pickle.dump(np.array(model_chains_h0), pklfile)

def run_fold_model_sampling():
    L, NL = 1000.0, 30
    M, Nsamp, Nburn, Nthin, temper = 4, 100000, 20000, 10, False
    model_chains_h0, model_chains_h1 = [ ], [ ]
    estimated_model_runtime = M * Nsamp * 360/1e+5

def sampling_table():
    output = ""
    for i, pars in enumerate(fault_model_pars):
        basefname = "implicit_{}_chains".format(i)
        for h in range(2):
            akalabels = ['   ', '-AA']
            ext =  "_h{}.pkl".format(h)
            with open(basefname + ext, 'rb') as pklfile:
                print("opening: {}".format(basefname+ext))
                chains = pickle.load(pklfile) # [-1]
                taus = [emcee.autocorr.integrated_time(ch, quiet=True) for ch in chains]
                taus = np.array(taus) * 0.01
                tmean, tmin, tmax = np.mean(taus), np.min(taus), np.max(taus)
                Rhat = gelman_rubin(chains)
                Rmean, Rmin, Rmax = np.mean(Rhat), np.min(Rhat), np.max(Rhat)
                output += (
                    "    {}{} & {:.1f} ({:.1f}, {:.1f}) $\\times 10^{{3}}$"
                    " & {:.2f} ({:.2f}, {:.2f}) \\\\\n".format
                    (i, akalabels[h], tmean, tmin, tmax, Rmean, Rmin, Rmax)
                )
    print(output)

def traceplots(basepklfname):
    """
    Make some trace plots for the paper
    :param pklfname: filename for pickled chains
    :return: N/A
    """
    # Chains array is of shape [Nchains, Nsamples, Npars]
    with open(basepklfname + "_h0.pkl", 'rb') as pklfile:
        chains_h0 = pickle.load(pklfile)
    with open(basepklfname + "_h1.pkl", 'rb') as pklfile:
        chains_h1 = pickle.load(pklfile)
    for i, ylabel in enumerate(fault_model_parnames):
        plt.figure(figsize=(5,7))
        plt.subplot(2, 1, 1)
        plt.plot(chains_h0[:,:,i].T)
        plt.title("Chain Traces for Model 5 (Aliased)")
        plt.xlabel('Sample Number ($\\times 100$)')
        plt.ylabel(ylabel)
        plt.subplot(2, 1, 2)
        plt.plot(chains_h1[:,:,i].T)
        plt.title("Chain Traces for Model 5 (Anti-Aliased)")
        plt.xlabel('Sample Number ($\\times 100$)')
        plt.ylabel(ylabel)
        plt.subplots_adjust(top=0.92, bottom=0.08, left=0.16, hspace=0.35)
        plt.savefig("implicit_5_trace_{}.eps".format(i))
        # plt.show()

def slice_figures(model_idx):

    # Initialize two GeoModels at different resolutions but same parameters
    L, NL, xmag, Nz = 1000.0, 15, 5, 30
    geopars = fault_model_pars[model_idx]
    model = initialize_geomodel(geopars, L, NL)
    model_hires = initialize_geomodel(geopars, L, NL*xmag)
    # Extract the forward models from these and make the data agree
    model.dsynth = model_hires.dsynth

    # Set up the three-column plots
    fwdmodels = [model.fwdmodel, model_hires.fwdmodel,
                 model.fwdmodel, model_hires.fwdmodel]
    akasettings = [False, False, True, True]

    # Actual parameters passed to plotting routine
    sliceparlist = [
        (model, 1,  3, 600.0, 200.0, Nz), # widths of layers on top of basement
        (model, 7, 12,  25.0,  25.0, Nz), # the two fault elevation angles
        (model, 9, 14, 200.0, 200.0, Nz), # the two fault displacements
        (model, 3,  7, 200.0,  25.0, Nz), # layer width vs elevation angle
        (model, 3, 14, 200.0, 200.0, Nz), # layer width vs displacement
        (model, 7, 14,  25.0, 200.0, Nz), # elevation angle vs displacement
    ]
    rowlabels = "abcdef"
    collabels = ["Coarse Aliased", "Fine Aliased",
                 "Coarse Anti-Aliased", "Fine Anti-Aliased"]

    # First row should be slices
    fig = plt.figure(figsize=(14, 3))
    for i in range(len(fwdmodels)):
        fm, aka = fwdmodels[i], akasettings[i]
        model.set_fwdmodel(fm)
        model.set_antialiasing(aka)
        profile_timer(fm.calc_gravity, model.h)
        # Plot the cross-section
        ax1 = plt.subplot(1, len(fwdmodels), i + 1)
        fm.plot_model_slice(ax=ax1, axlabels=(i == len(fwdmodels)-1),
                            grid=(i % 2 == 0))
        ax1.set_title(collabels[i])
        ax1.set_xlabel("x (m)")
        if i == 0:
            plt.ylabel("z (m)")
        else:
            plt.ylabel("")

    # plt.subplots_adjust(top=0.85, bottom=0.15, hspace=0.35, wspace=0.35)
    plt.subplots_adjust(bottom=0.2, left=0.08, right=0.92, wspace=0.35)
    figfn = "sliceplots/implicit_{}_slices.eps".format(model_idx)
    plt.savefig(figfn)

    # Now scan through all the posteriors
    xg, yg, Lg, args = [ ], [ ], [ ], [ ]
    for j, slicepars in enumerate(sliceparlist):
        fig = plt.figure(figsize=(14, 3))
        for i in range(len(fwdmodels)):
            fm, aka = fwdmodels[i], akasettings[i]
            model.set_fwdmodel(fm)
            model.set_antialiasing(aka)
            ax = plt.subplot(1, len(fwdmodels), i + 1)
            xgji, ygji, Lgji = vet_slice(*slicepars)
            plt.xlabel(fault_model_parnames[slicepars[1]])
            if i == 0:
                plt.ylabel(fault_model_parnames[slicepars[2]])
            args.append(slicepars[1:])
            xg.append(xgji)
            yg.append(ygji)
            Lg.append(Lgji)
        # plt.subplots_adjust(top=0.85, bottom=0.15, hspace=0.35, wspace=0.35)
        plt.subplots_adjust(bottom=0.2, left=0.08, right=0.92, wspace=0.35)
        figfn = "sliceplots/implicit_{}{}_slices.eps".format(
            model_idx, rowlabels[j])
        plt.savefig(figfn)

    pklfn = "sliceplots/implicit_{}_slices.pkl".format(model_idx)
    with open(pklfn, 'wb') as pklfile:
        pickle.dump([xg, yg, Lg, args], pklfile)


if __name__ == "__main__":
    # for model_idx in range(len(fault_model_pars)):
    #     slice_figures(model_idx)
    # profile_timer(run_fault_model_sampling)
    traceplots("chainplots/amrw_stepsize=v2_thin10_t5/implicit_5_chains")
    # traceplots("chainplots/amrw_stepsize=v2_thin10_t5/implicit_5_chains")


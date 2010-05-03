# equil.py
# Copyright (c) 2010 Oliver Beckstein

"""
:mod:`mdpow.equil` --- Setting up and running equilibrium MD
============================================================

The :mod:`mdpow.equil` module facilitates the setup of equilibrium
molecular dynamics simulations of a compound molecule in a simulation
box of water or other solvent such as octanol.

It requires as input

 - the itp file for the compound
 - a coordinate (structure) file (in pdb or gro format)

By default it uses the *OPLS/AA* forcefield and the *TIP4P* water
model.

.. autoclass:: Simulation
   :members:
.. autoclass:: WaterSimulation
.. autoclass:: OctanolSimulation

.. autodata:: ITP
.. autodata:: BOX
.. autodata:: DIST
"""

from __future__ import with_statement

import os, errno
import shutil
import cPickle
import logging

import gromacs.setup
import gromacs.cbook
from gromacs.utilities import in_dir, realpath, asiterable, AttributeDict
import gromacs.utilities

import config

logger = logging.getLogger('mdpow.equil')

#: itp files are picked up via include dirs
ITP = {'water': 'tip4p.itp', 'octanol': '1oct.itp'}
#: solvent boxes
BOX = {'water': 'tip4p.gro', 'octanol': config.topfiles['1oct.gro']}
#: minimum distance between solute and box surface (in nm)
DIST = {'water': 1.0, 'octanol': 1.5}

class Simulation(object):
    """Simple MD simulation of a single compound molecule in water.

    Typical use ::

       S = Simulation(molecule='DRUG')
       S.topology(itp='drug.itp')
       S.solvate(struct='DRUG-H.pdb')
       S.energy_minimize()
       S.MD_relaxed()
       S.MD()

    .. Note:: The OPLS/AA force field and the TIP4P water molecule is the
              default; changing this is possible but will require provision of
              customized itp and mdp files at various stages.
    """

    #: Keyword arguments to pre-set some file names; they are keys in :attr:`Simulation.files`.
    filekeys = ('topology', 'processed_topology', 'structure', 'solvated', 'ndx', 
                'energy_minimized', 'MD_relaxed', 'MD_restrained', 'MD_NPT')
    dirname_default = os.path.curdir
    solvent_default = 'water'

    #: Coordinate files of the full system in increasing order of advancement of
    #: the protocol; the later the better. The values are keys into :attr:`Simulation.files`.
    coordinate_structures = ('solvated', 'energy_minimized', 'MD_relaxed',  
                             'MD_restrained', 'MD_NPT')
    
    def __init__(self, molecule=None, **kwargs):
        """Set up Simulation instance.

        The *molecule* of the compound molecule should be supplied. Existing files
        (which have been generated in previous runs) can also be supplied.

        :Keywords:
          *molecule*
              Identifier for the compound molecule. This is the same as the
              entry in the ``[ molecule ]`` section of the itp file. ["DRUG"]
          *filename*
              If provided and *molecule* is ``None`` then load the instance from
              the pickle file *filename*, which was generated with
              :meth:`~mdpow.equil.Simulation.save`.
          *dirname*
              base directory; all other directories are created under it
          *solvent*
              water or octanol
          *kwargs*
              advanced keywords for short-circuiting; see
              :data:`mdpow.equil.Simulation.filekeys`.
              
        """
        filename = kwargs.pop('filename', None)
        dirname = kwargs.pop('dirname', self.dirname_default)
        solvent = kwargs.pop('solvent', self.solvent_default)
        if molecule is None and not filename is None:
            # load from pickle file
            self.load(filename)
            self.filename = filename            
            kwargs = {}    # for super
        else:
            self.molecule = molecule or 'DRUG'
            self.dirs = AttributeDict(
                basedir=realpath(dirname),
                includes=list(asiterable(kwargs.pop('includes',[]))) + [config.includedir],
                )
            # pre-set filenames: keyword == variable name
            self.files = AttributeDict([(k, kwargs.pop(k, None)) for k in self.filekeys])

            if self.files.topology:
                # assume that a user-supplied topology lives in a 'standard' top dir
                # that includes the necessary itp file(s)
                self.dirs.topology = realpath(os.path.dirname(self.files.topology))
                self.dirs.includes.append(self.dirs.topology)

            self.solvent_type = solvent
            try:
                self.solvent = AttributeDict(itp=ITP[solvent], box=BOX[solvent],
                                             distance=DIST[solvent])
            except KeyError:
                raise ValueError("solvent must be one of %r" % ITP.keys())

            self.filename = filename or self.solvent_type+'.simulation'

        super(Simulation, self).__init__(**kwargs)

    def BASEDIR(self, *args):
        return os.path.join(self.dirs.basedir, *args)

    def save(self, filename=None):
        """Save instance to a pickle file.

        The default filename is the name of the file that was last loaded from
        or saved to.
        """
        if filename is None:
            if self.filename is None:
                self.filename = filename or self.solvent_type+'.simulation'
                logger.warning("No filename known, saving instance under name %r", self.filename)
            filename = self.filename
        else:
            self.filename = filename
        with open(filename, 'wb') as f:
            cPickle.dump(self, f, protocol=cPickle.HIGHEST_PROTOCOL)
        logger.debug("Instance pickled to %(filename)r" % vars())
        
    def load(self, filename=None):
        """Re-instantiate class from pickled file."""
        if filename is None:
            if self.filename is None:
                self.filename = self.molecule.lower() + '.pickle'
                logger.warning("No filename known, trying name %r", self.filename)
            filename = self.filename        
        with open(filename, 'rb') as f:
            instance = cPickle.load(f)
        self.__dict__.update(instance.__dict__)
        logger.debug("Instance loaded from %(filename)r" % vars())

    def make_paths_relative(self, prefix=os.path.curdir):
        """Hack to be able to copy directories around: prune basedir from paths.

        .. Warning:: This is not guaranteed to work for all paths. In particular,
                     check :data:`mdpow.equil.Simulation.dirs.includes` and adjust
                     manually if necessary.
        """
        def assinglet(m):
            if len(m) == 1:
                return m[0]
            elif len(m) == 0:
                return None
            return m
        
        basedir = self.dirs.basedir
        for key, fn in self.files.items():
            try:
                self.files[key] = fn.replace(basedir, prefix)
            except AttributeError:
                pass
        for key, val in self.dirs.items():
            fns = asiterable(val)  # treat them all as lists
            try:
                self.dirs[key] = assinglet([fn.replace(basedir, prefix) for fn in fns])
            except AttributeError:
                pass

    def topology(self, itp='drug.itp', **kwargs):
        """Generate a topology for compound *molecule*.

        :Keywords:
            *itp*
               Gromacs itp file; will be copied to topology dir and
               included in topology
            *dirname*
               name of the topology directory ["top"]
            *kwargs*
               see source for *top_template*, *topol*
        """
        dirname = kwargs.pop('dirname', self.BASEDIR('top'))
        self.dirs.topology = realpath(dirname)
        
        top_template = config.get_template(kwargs.pop('top_template', 'system.top'))
        topol = kwargs.pop('topol', os.path.basename(top_template))
        itp = os.path.realpath(itp)
        _itp = os.path.basename(itp)

        with in_dir(dirname):
            shutil.copy(itp, _itp)
            gromacs.cbook.edit_txt(top_template,
                                   [('#include +"compound\.itp"', 'compound\.itp', _itp),
                                    ('#include +"tip4p\.itp"', 'tip4p\.itp', self.solvent.itp),
                                    ('Compound', 'solvent', self.solvent),
                                    ('Compound', 'DRUG', self.molecule),
                                    ('DRUG\s*1', 'DRUG', self.molecule),
                                    ],
                                   newname=topol)
        logger.info('[%(dirname)s] Created topology %(topol)r that includes %(_itp)r', vars())

        # update known files and dirs
        self.files.topology = realpath(dirname, topol)
        if not self.dirs.topology in self.dirs.includes:
            self.dirs.includes.append(self.dirs.topology)
        
        return {'dirname': dirname, 'topol': topol}


    def solvate(self, struct=None, **kwargs):
        """Solvate structure *struct* in a box of solvent.

        The solvent is determined with the *solvent* keyword to the constructor.

        :Keywords:
          *struct*
              pdb or gro coordinate file (if not supplied, the value is used
              that was supplied to the constructor of :class:`~mdpow.equil.Simulation`)
          *kwargs*
              All other arguments are passed on to :func:`gromacs.setup.solvate`, but
              set to sensible default values. *top* and *water* are always fixed.
        """
        self.dirs.solvation = realpath(kwargs.setdefault('dirname', self.BASEDIR('solvation')))
        kwargs['struct'] = self._checknotempty(struct or self.files.structure, 'struct')
        kwargs['top'] = self._checknotempty(self.files.topology, 'top')
        kwargs['water'] = self.solvent.box
        kwargs.setdefault('mainselection', '"%s"' % self.molecule)  # quotes are needed for make_ndx
        kwargs.setdefault('distance', self.solvent.distance)
        kwargs['includes'] = asiterable(kwargs.pop('includes',[])) + self.dirs.includes

        params = gromacs.setup.solvate(**kwargs)

        self.files.structure = kwargs['struct']
        self.files.solvated = params['struct']
        self.files.ndx = params['ndx']

        # we can also make a processed topology right now
        self.processed_topology()
        
        return params

    def processed_topology(self, **kwargs):
        """Create a portable topology file from the topology and the solvated system."""
        if self.files.solvated is None or not os.path.exists(self.files.solvated):
            self.solvate(**kwargs)
        self.files.processed_topology = gromacs.cbook.create_portable_topology(
            self.files.topology, self.files.solvated, includes=self.dirs.includes)
        return self.files.processed_topology

    def energy_minimize(self, **kwargs):
        """Energy minimize the solvated structure on the local machine.

        *kwargs* are passed to :func:`gromacs.setup.energ_minimize` but if
        :meth:`~mdpow.equil.Simulation.solvate` step has been carried out
        previously all the defaults should just work.
        """
        self.dirs.energy_minimization = realpath(kwargs.setdefault('dirname', self.BASEDIR('em')))
        kwargs['top'] = self.files.topology
        kwargs.setdefault('struct', self.files.solvated)
        kwargs['mainselection'] = None
        kwargs['includes'] = asiterable(kwargs.pop('includes',[])) + self.dirs.includes

        params = gromacs.setup.energy_minimize(**kwargs)

        self.files.energy_minimized = params['struct']
        return params

    def _MD(self, protocol, **kwargs):
        """Basic MD driver for this Simulation. Do not call directly."""
        assert protocol in self.filekeys    # simple check (XXX: should only check a subset,not all keys)

        kwargs.setdefault('dirname', self.BASEDIR(protocol))
        self.dirs[protocol] = realpath(kwargs['dirname'])
        MD = kwargs.pop('MDfunc', gromacs.setup.MD)
        kwargs['top'] = self.files.topology
        kwargs['includes'] = asiterable(kwargs.pop('includes',[])) + self.dirs.includes
        kwargs['mdp'] = config.get_template('NPT_opls.mdp')
        kwargs['ndx'] = self.files.ndx
        kwargs['mainselection'] = None # important for SD (use custom mdp and ndx!, gromacs.setup._MD)
        self._checknotempty(kwargs['struct'], 'struct')
        if not os.path.exists(kwargs['struct']):
            # struct is not reliable as it depends on qscript so now we just try everything...
            struct = gromacs.utilities.find_first(kwargs['struct'], suffices=['pdb', 'gro'])
            if struct is None:
                logger.error("Starting structure %(struct)r does not exist (yet)" % kwargs)
                raise IOError(errno.ENOENT, "Starting structure not found", kwargs['struct'])
            else:
                logger.info("Found starting structure %r (instead of %r).", struct, kwargs['struct'])
                kwargs['struct'] = struct
        params =  MD(**kwargs)
        # params['struct'] is md.gro but could also be md.pdb --- depends entirely on qscript
        self.files[protocol] = params['struct']
        return params

    def MD_relaxed(self, **kwargs):
        """Short MD simulation with *timestep* = 0.1 fs to relax strain.

        Energy minimization does not always remove all problems and LINCS
        constraint errors occur. A very short *runtime* = 5 ps MD with very
        short integration time step *dt* tends to solve these problems.
        """
        # user structure or restrained or solvated
        kwargs.setdefault('struct', self.files.energy_minimized)
        kwargs.setdefault('dt', 0.0001)  # ps
        kwargs.setdefault('runtime', 5)  # ps
        return self._MD('MD_relaxed', **kwargs)

    def MD_restrained(self, **kwargs):
        """Short MD simulation with position restraints on compound.

        See documentation of :func:`gromacs.setup.MD_restrained` for
        details. The following keywords can not be changed: top, mdp, ndx,
        mainselection

        .. Note:: Position restraints are activated with ``-DPOSRES`` directives
                  for :func:`gromacs.grompp`. Hence this will only work if the
                  compound itp file does indeed contain a ``[ posres ]``
                  section that is protected by a ``#ifdef POSRES`` clause.
        """
        kwargs.setdefault('struct', 
                          self._lastnotempty([self.files.energy_minimized, self.files.MD_relaxed]))
        kwargs['MDfunc'] = gromacs.setup.MD_restrained
        return self._MD('MD_restrained', **kwargs)

    def MD(self, **kwargs):
        """Short NPT MD simulation.

        See documentation of :func:`gromacs.setup.MD` for details such
        as *runtime* or specific queuing system options. The following
        keywords can not be changed: top, mdp, ndx, mainselection.

        .. Note:: If the system crashes (with LINCS errors), try initial
                  equilibration with timestep *dt* = 0.0001 ps (0.1 fs instead
                  of 2 fs) and *runtime* = 5 ps.

        :Keywords:
          *struct*
               starting conformation; by default, the *struct* is the last frame
               from the position restraints run, or, if this file cannot be
               found (e.g. because :meth:`Simulation.MD_restrained` was not run)
               it falls back to the relaxed and then the solvated system.
          *runtime*
               total run time in ps
          *qscript*
               list of queuing system scripts to prepare; available values are
               in :data:`gromacs.config.templates` or you can provide your own
               filename(s) in the current directory (see :mod:`gromacs.qsub` for
               the format of the templates)
        """
        # user structure or relaxed or restrained or solvated
        kwargs.setdefault('struct', self.get_last_structure())
        return self._MD('MD_NPT', **kwargs)
        

    def _checknotempty(self, value, name):
        if value is None or value == "":
            raise ValueError("Parameter %s cannot be empty." % name)
        return value

    def _lastnotempty(self, l):
        """Return the last non-empty value in list *l* (or None :-p)"""
        nonempty = [None] + [x for x in l if not (x is None or x == "" or x == [])]
        return nonempty[-1]

    def get_last_structure(self):
        """Returns the coordinates of the most advanced step in the protocol."""
        return self._lastnotempty([self.files[name] for name in self.coordinate_structures])

class WaterSimulation(Simulation):
    """Equilibrium MD of a solute in a box of water."""
    dirname_default = os.path.join('Equilibrium/water')
    solvent_default = 'water'

class OctanolSimulation(Simulation):
    """Equilibrium MD of a solute in a box of octanol."""
    dirname_default = os.path.join('Equilibrium/octanol')
    solvent_default = 'octanol'

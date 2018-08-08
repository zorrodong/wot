# -*- coding: utf-8 -*-

from multiprocessing import Process
from wot.population import Population
import wot.model
import wot.io
import os
import itertools
import numpy as np
import pandas as pd

class OTModel:
    """
    The OTModel takes care of computing and properly caching the transport maps
    needed when necessary. All computations using transport maps should be
    performed through the OTModel.

    Parameters
    ----------
    matrix : wot.Dataset
        The gene expression matrix for this OTModel
    transport_maps_directory : str
        Path to the transport map directory, where already computed transport
        maps are stored and future transport maps may be cached.
    transport_maps_prefix : str, optional
        Prefix to use for the transport maps. This can highly speed up
        initialization if the directory is filled with other non-tmap files,
        and allows to have several transport maps not overriding each other.
        If None, all files named `{prefix}_{t0}_{t1}.{extension}` will be
        considered as transport maps.
        The default prefix for cached transport maps is 'tmaps'
    max_threads : int, optional
        Maximum number of threads to use when computing transport maps
    **kwargs : dict
        Dictionnary of parameters. Will be inserted as is into OT configuration.
    """

    default_tmap_prefix = "tmaps"

    def __init__(self, matrix, tmap_dir = None, tmap_prefix = None, max_threads = None, **kwargs):
        self.matrix = matrix
        self.timepoints = sorted(set(matrix.row_meta['day']))
        self.tmap_dir = tmap_dir or '.'
        self.tmap_prefix = tmap_prefix or self.default_tmap_prefix

        wot.io.verbose("Initializing OTModel ({},{})".format(tmap_dir, tmap_prefix))
        wot.io.verbose("Additional arguments :", kwargs)
        wot.io.verbose(len(self.timepoints), "timepoints loaded :", self.timepoints)

        self.day_pairs = wot.model.parse_configuration(kwargs.pop('day_pairs', None))
        cov = kwargs.pop('covariate', None)
        if cov is not None:
            covariate_data_frame = wot.io.read_covariate_data_frame(cov)
            self.matrix.row_meta = self.matrix.row_meta.join(covariate_data_frame)

        g = kwargs.pop('cell_growth_rates', None)
        if g is not None:
            g_data_frame = wot.io.read_covariate_data_frame(g)
            self.matrix.row_meta = self.matrix.row_meta.join(g_data_frame)

        cell_filter = kwargs.pop('cell_filter', None)
        gene_filter = kwargs.pop('gene_filter', None)
        if gene_filter is not None:
            if os.path.isfile(gene_filter):
                gene_ids = pd.read_table(gene_filter, index_col=0, header=None)\
                        .index.values
            else:
                import re
                expr = re.compile(gene_filter)
                gene_ids = [e for e in self.matrix.col_meta.index.values if expr.match(e)]
            col_indices = self.matrix.col_meta.index.isin(gene_ids)
            self.matrix = wot.Dataset(self.matrix.x[:,col_indices],
                    self.matrix.row_meta, self.matrix.col_meta[col_indices])
            wot.io.verbose('Successfuly applied gene_filter: "{}"'.format(gene_filter))
        if cell_filter is not None:
            if os.path.isfile(cell_filter):
                cell_ids = pd.read_table(cell_filter, index_col=0, header=None)\
                        .index.values
            else:
                import re
                expr = re.compile(cell_filter)
                cell_ids = [e for e in self.matrix.row_meta.index.values if expr.match(e)]
            row_indices = self.matrix.row_meta.index.isin(cell_ids)
            self.matrix = wot.Dataset(self.matrix.x[row_indices,:],
                    self.matrix.row_meta[row_indices], self.matrix.col_meta)
            wot.io.verbose('Successfuly applied cell_filter: "{}"'.format(cell_filter))

        if max_threads is None or max_threads == 0:
            wot.io.verbose("Argument max_threads not set. Using default")
            max_usable_cores = len(os.sched_getaffinity(0))
            if kwargs.pop('fast', False):
                wot.io.verbose("Fast mode. Using all but one cores")
                self.max_threads = max_usable_cores - 1
            else:
                self.max_threads = 1
        else:
            self.max_threads = max_threads
        wot.io.verbose("Using", self.max_threads, "thread(s) at most")
        if self.max_threads > 1:
            wot.io.verbose("Warning : Multiple threads are being used. Time estimates will be inaccurate")

        self.ot_config = {}
        for k in kwargs.keys():
            self.ot_config[k] = kwargs[k]

        wot.model.check_model_consistency(self)

        wot.model.purge_invalidated_caches(self)
        self.tmaps, self.cov_tmaps = wot.model.scan_transport_map_directory(self)


    def get_ot_config(self):
        """
        Get valid parameters for the Optimal Transport computation.

        Returns
        -------
        ot_config : dict
            Dictionnary of valid parameters, or defaults if unspecified.
        """
        # WARNING: Any value in ot_config that does not appear in the following dict will be ignored
        ot_defaults = {
                'epsilon': .05, 'lambda1': 1, 'lambda2': 50,
                'epsilon0': 1, 'tau': 1e4,
                'growth_iters': 3, 'batch_size': 50,
                'local_pca': 30, 'max_iter': 1e7,
                'tolerance': 1e-2,
                }
        # TODO: support gene_filter and cell_filter
        # TODO: support ncells and ncounts
        config = self.ot_config

        return { x: config[x] if x in config else ot_defaults[x] for x in ot_defaults }

    def get_covariate_pairs(self):
        """Get all covariate pairs in the dataset"""
        if 'covariate' not in self.matrix.row_meta.columns:
            raise ValueError("Covariate value not available in dataset")
        from itertools import product
        covariate = sorted(set(self.matrix.row_meta['covariate']))
        return product(covariate, covariate)


    def compute_all_transport_maps(self, force = False, with_covariates=False):
        """
        Computes all required transport maps and caches everything for future use.

        Parameters
        ----------
        force : bool, optional, default : False
            Force recomputation of each transport map, after config update for instance.
        with_covariates : bool, optional, default : False
            Compute all covariate-restricted transport maps as well

        Returns
        -------
        None
            Only computes and caches all transport maps, does not return them.
        """
        t = self.timepoints
        day_pairs = self.day_pairs
        if day_pairs is None:
            day_pairs = [(t[i], t[i+1]) for i in range(len(t) - 1)]

        if with_covariates:
            day_pairs = [(*d, c) for d, c in itertools.product(day_pairs, self.get_covariate_pairs())]

        if not force:
            if with_covariates:
                day_pairs = [(t0, t1, cv) for t0, t1, cv in day_pairs
                        if self.cov_tmaps.get((t0,t1,*cv), None) is None]
            else:
                day_pairs = [x for x in day_pairs if self.tmaps.get(x, None) is None]

        m = self.max_threads

        if not day_pairs:
            return

        if m > 1 :
            procs = []
            for x in day_pairs:
                p = Process(target=self.compute_transport_map, args=(*x,))
                procs.append(p)

            for i in range(len(procs) + m):
                if i >= m :
                    procs[i - m].join()
                if i < len(procs):
                    procs[i].start()
            self.tmaps, self.cov_tmaps = wot.model.scan_transport_map_directory(self)
        else:
            for x in day_pairs :
                self.compute_transport_map(*x)

    def compute_transport_map(self, t0, t1, covariate=None):
        """
        Computes the transport map from time t0 to time t1

        Parameters
        ----------
        t0 : float
            Source timepoint for the transport map
        t1 : float
            Destination timepoint for the transport map
        covariate : None or (int, int)
            The covariate restriction on cells from t0 and t1. None to skip

        Returns
        -------
        None
            Only computes and caches the transport maps, does not return it.

        Raises
        ------
        ValueError
            If the OTModel was initialized with day_pairs and the given pair is not present.
        """
        if self.tmap_prefix is None:
            path = self.default_tmap_prefix
        else:
            path = self.tmap_prefix

        # If day_pairs is not None, its configuration takes precedence
        if self.day_pairs is not None:
            if (t0, t1) not in self.day_pairs:
                raise ValueError("Transport map ({},{}) is not present in day_pairs".format(t0, t1))
            local_config = self.day_pairs[(t0, t1)]
        else:
            local_config = {}

        config = { **self.get_ot_config(), **local_config, 't0': t0, 't1': t1, 'covariate': covariate }
        tmap = wot.ot.OptimalTransportHelper.compute_single_transport_map(self.matrix, config)
        if covariate is None:
            path += "_{}_{}.loom".format(t0, t1)
            self.tmaps[(t0, t1)] = path
        else:
            path += "_{}_{}_cv{}_cv{}.loom".format(t0, t1, *covariate)
            self.cov_tmaps[(t0, t1, *covariate)] = path

        wot.io.write_dataset(tmap, os.path.join(self.tmap_dir, path),
                output_format="loom", txt_full=False)
        wot.io.verbose("Cached tmap ({}, {}) : {}".format(t0, t1, path))

    def transport_map(self, t0, t1, covariate=None):
        """
        Loads a transport map for a given pair of timepoints.

        Parameters
        ----------
        t0 : int or float
            Source timepoint of the transport map.
        t1 : int of float
            Destination timepoint of the transport map.
        covariate : None or (int, int), optional
            Restrict to certain covariate values. Do not restrict if None

        Returns
        -------
        tmap : wot.Dataset
            The transport map from t0 to t1
        """
        if t0 not in self.timepoints or t1 not in self.timepoints:
            raise ValueError("Timepoints {}, {} not found".format(t0, t1))

        atomic = (self.day_pairs is not None and (t0,t1) in self.day_pairs)\
                or self.timepoints.index(t1) == self.timepoints.index(t0) + 1

        if not atomic and covariate is not None:
            raise ValueError("Covariate-restricted transport maps can only be atomic")

        if atomic:
            if covariate is None:
                return wot.model.load_transport_map(self, t0, t1)
            else:
                return wot.model.load_covariate_restricted_transport_map(self, t0, t1, covariate)
        else:
            path = wot.model.find_path(t0, t1, self.day_pairs, self.timepoints)
            return wot.model.chain_transport_maps(self, path)

    def can_push_forward(self, *populations):
        """
        Checks if the populations can be pushed forward.

        Parameters
        ----------
        *populations : wot.Population
            Measure over the cells at a given timepoint to be pushed forward.

        Returns
        -------
        result : bool
            True if the populations can be pushed forward

        Raises
        ------
        ValueError
            If all populations are not in the same timepoint
        """
        return self.timepoints.index(wot.model.unique_timepoint(*populations)) \
                < len(self.timepoints) - 1

    def can_pull_back(self, *populations):
        """
        Checks if the populations can be pulled back.

        Parameters
        ----------
        *populations : wot.Population
            Measure over the cells at a given timepoint to be pulled back.

        Returns
        -------
        result : bool
            True if the populations can be pulled back.

        Raises
        ------
        ValueError
            If all populations are not in the same timepoint
        """
        return self.timepoints.index(wot.model.unique_timepoint(*populations)) > 0

    def push_forward(self, *populations, to_time = None, normalize=True, as_list=False):
        """
        Pushes the population forward through the computed transport maps

        Parameters
        ----------
        *populations : wot.Population
            Measure over the cells at a given timepoint to be pushed forward.
        to_time : int or float, optional
            Destination timepoint to push forward to.
        normalize : bool, optional, default: True
            Wether to normalize to a probability distribution or keep growth.
        as_list : bool, optional, default: False
            Wether to return a listof length 1 when a single element is passed, or a Population

        Returns
        -------
        result : wot.Population
            The push forward of the input population through the proper transport map.
            Array of populations if several populations were given as input.

        Raises
        ------
        ValueError
            If there is no further timepoint to push the population forward.
        ValueError
            If several populations are given as input but dot live in the same timepoint.

        Examples
        --------
        >>> ot_model.push_forward(pop, to_time = 2) # -> wot.Population
        Pushing several populations at once
        >>> ot_model.push_forward(pop1, pop2, pop3) # -> list of wot.Population
        Pulling back after pushing forward
        >>> ot_model.pull_back(ot_model.push_forward(pop))
        Same, but several populations at once
        >>> ot_model.pull_back(* ot_model.push_forward(pop1, pop2, pop3))
        """
        i = self.timepoints.index(wot.model.unique_timepoint(*populations))
        j = i + 1 if to_time is None else self.timepoints.index(to_time)

        if i == -1:
            raise ValueError("Timepoint not found")
        if j == -1:
            raise ValueError("Destination timepoint not found")
        if j >= len(self.timepoints):
            raise ValueError("No further timepoints. Unable to push forward")
        if i > j :
            raise ValueError("Destination timepoint is before source. Unable to push forward")

        p = np.vstack([ pop.p for pop in populations ])
        while i < j:
            t0 = self.timepoints[i]
            t1 = self.timepoints[i+1]
            tmap = self.transport_map(t0, t1)
            p = np.dot(p, tmap.x)
            if normalize:
                p = (p.T / np.sum(p, axis=1)).T
            i += 1

        result = [ Population(self.timepoints[i], p[k,:]) for k in range(p.shape[0]) ]
        if len(result) == 1 and not as_list:
            return result[0]
        else:
            return result

    def pull_back(self, *populations, to_time = None, normalize=True, as_list=False):
        """
        Pulls the population back through the computed transport maps

        Parameters
        ----------
        *populations : wot.Population
            Measure over the cells at a given timepoint to be pushed forward.
        to_time : int or float, optional
            Destination timepoint to pull back to.
        normalize : bool, optional, default: True
            Wether to normalize to a probability distribution or keep growth.
        as_list : bool, optional, default: False
            Wether to return a listof length 1 when a single element is passed, or a Population

        Returns
        -------
        result : wot.Population
            The pull back of the input population through the proper transport map.
            Array of populations if several populations were given as input.

        Raises
        ------
        ValueError
            If there is no previous timepoint to pull the population back.
        ValueError
            If several populations are given as input but dot live in the same timepoint.

        Examples
        --------
        >>> ot_model.pull_back(pop, to_time = 0) # -> wot.Population
        Pushing several populations at once
        >>> ot_model.pull_back(pop1, pop2, pop3) # -> list of wot.Population
        Pulling back after pushing forward
        >>> ot_model.pull_back(ot_model.push_forward(pop))
        Same, but several populations at once
        >>> ot_model.pull_back(* ot_model.push_forward(pop1, pop2, pop3))
        """
        i = self.timepoints.index(wot.model.unique_timepoint(*populations))
        j = i - 1 if to_time is None else self.timepoints.index(to_time)

        if i == -1:
            raise ValueError("Timepoint not found")
        if i == 0:
            raise ValueError("No previous timepoints. Unable to pull back")
        if j == -1:
            raise ValueError("Destination timepoint not found")
        if i < j :
            raise ValueError("Destination timepoint is after source. Unable to pull back")

        p = np.vstack([ pop.p for pop in populations ])
        while i > j:
            t1 = self.timepoints[i]
            t0 = self.timepoints[i-1]
            tmap = self.transport_map(t0, t1)
            p = np.dot(tmap.x, p.T).T
            if normalize:
                p = (p.T / np.sum(p, axis=1)).T
            i -= 1

        result = [ Population(self.timepoints[i], p[k,:]) for k in range(p.shape[0]) ]
        if len(result) == 1 and not as_list:
            return result[0]
        else:
            return result

    def ancestors(self, *populations, at_time=None, as_list=False):
        """
        Computes the ancestors of a given population by pulling back through transport maps

        Parameters
        ----------
        *populations : wot.Population
            Measure over the cells at a given timepoint to compute ancestors for.
        at_time : int or float, optional
            Timepoint for which to compute the ancestors.
            If None, compute ancestors for the previous available time point.
        as_list : bool, optional, default: False
            Wether to return a listof length 1 when a single element is passed, or a Population

        Returns
        -------
        ancestors : wot.Population or list of wot.Population
            A population of cells, at the destination timepoint, most likely to be the ancestors of the input population.
            List if several populations were given, single population otherwise.

        Raises
        ------
        ValueError
            If the selected destination timepoint does not exist.
        ValueError
            If the selected destination is after the original timepoint.

        Examples
        --------
        >>> ot_model.ancestors(pop, at_time = 0) # -> wot.Population
        # Using several populations at once
        >>> ot_model.ancestors(pop1, pop2, pop3) # -> list of wot.Population
        # Chaining ancestors and descendants
        >>> ot_model.ancestors(ot_model.descendants(pop))
        # Same, but several populations at once
        >>> ot_model.ancestors(* ot_model.descendants(pop1, pop2, pop3))

        Notes
        -----
        If population.time is 7 and at_time is 5, the OTModel would pull back through two transport maps.
        This method is only and alias to OTModel.pull_back
        """
        return self.pull_back(*populations, to_time = at_time, as_list=as_list)

    def descendants(self, *populations, at_time=None, as_list=False):
        """
        Computes the descendants of a given population by pushing forward through transport maps

        Parameters
        ----------
        *populations : wot.Population
            Measure over the cells at a given timepoint to compute ancestors for.
        at_time : int or float, optional
            Timepoint for which to compute the ancestors.
            If None, compute ancestors for the previous available time point.
        as_list : bool, optional, default: False
            Wether to return a listof length 1 when a single element is passed, or a Population

        Returns
        -------
        descendants : wot.Population or list of wot.Population
            A population of cells at the destination timepoint, most likely to be the descendants of the input population.
            List if several populations were given, single population otherwise.

        Raises
        ------
        ValueError
            If the selected destination timepoint does not exist.
        ValueError
            If the selected destination is before the original timepoint.

        Examples
        --------
        >>> ot_model.descendants(pop, at_time = 2) # -> wot.Population
        # Using several populations at once
        >>> ot_model.descendants(pop1, pop2, pop3) # -> list of wot.Population
        # Chaining ancestors and descendants
        >>> ot_model.ancestors(ot_model.descendants(pop))
        # Same, but several populations at once
        >>> ot_model.ancestors(* ot_model.descendants(pop1, pop2, pop3))

        Notes
        -----
        If population.time is 5 and at_time is 7, the OTModel would push forward through two transport maps.
        This method is only and alias to OTModel.push_forward
        """
        return self.push_forward(*populations, to_time = at_time, as_list=as_list)

    def population_from_ids(self, *ids, at_time=None):
        """
        Constructs a population uniformly distributed among the ids given as input.

        Parameters
        ----------
        *ids : list of str
            The list of cell ids that belong to that population.
        at_time : int or float, optional
            The time at which to construct the population.
            Cells that come from a different time point will be ignored.

        Returns
        -------
        *populations : wot.Population
            A population, uniformly distributed over the cells given as input.
            List if several lists of ids were given, single population otherwise.
            Returns None if the generated population would be empty

        Raises
        ------
        ValueError
            If at_time is not specified and all cells do not live in the same timepoint.

        Examples
        --------
        >>> cell_set = [ 'cell_1', 'cell_2', 'cell_3' ]
        >>> ot_model.population_from_ids(cell_set) # -> wot.Population
        Multiple populations at once
        >>> multi_cell_sets = {
        >>>   'set_a': [ 'cell_a1', 'cell_a2'],
        >>>   'set_b': [ 'cell_b1', 'cell_b2'],
        >>> }
        >>> ot_model.population_from_ids(* multi_cell_sets.values()) # -> list of wot.Population

        Notes
        -----
        The Population class is a measure over the cells at a given timepoint.
        It does not necessarily sum to 1. However, this method always returns a probability distribution over the cells of that time point.
        """
        day = at_time
        all_ids = [ i for ids_el in ids for i in ids_el ]
        cell_inds = self.matrix.row_meta.index.get_indexer_for(all_ids)

        if at_time is None:
            day = self.matrix.row_meta.loc[ids[0][0], 'day']
            if not all(self.matrix.row_meta.iloc[cell_inds]['day'] == day):
                raise ValueError("All cells do not live in the same timepoint. Please choose one")
        else:
            day = float(at_time)

        day_query = self.matrix.row_meta['day'] == day
        all_inds = np.where(day_query)[0]

        def get_population(ids_el):
            cell_inds = self.matrix.row_meta.index.get_indexer_for(ids_el)
            p = [ 1 if id in cell_inds else 0 for id in all_inds ]
            p = np.asarray(p, dtype=np.float64)
            if np.isclose(np.sum(p), 0):
                return None
            else:
                return Population(day, p / np.sum(p))

        result = [ get_population(ids_el) for ids_el in ids ]
        if len(result) == 1:
            return result[0]
        else:
            return result

    def population_from_cell_sets(self, cell_sets, at_time=None):
        """
        Similar to population_from_ids for cell sets

        Parameters
        ----------
        cell_sets : dict of str: list of str
            The dictionnary of ids
        at_time : float, optional
            The timepoint to consider

        Returns
        -------
        populations : dict of str: wot.Population
            The resulting populations
        """
        keys = list(cell_sets.keys())
        populations = self.population_from_ids(*[cell_sets[name] for name in keys], at_time=at_time)
        return { keys[i]: populations[i] for i in range(len(keys)) if populations[i] is not None }


    def cell_ids(self, population):
        day = population.time
        return list(self.matrix.row_meta.index[self.matrix.row_meta['day'] == day])

    def population_census(self, cell_set_matrix, *populations):
        """
        Get a census for a population with respect to a given cell set matrix

        Parameters
        ----------
        cell_set_matrix : wot.Dataset
            Dataset of 0s and 1s denoting membership in each cell set.
            Cells as rows, cell sets as columns.
        *populations : wot.Population or list of wot.Population
            The population to be considered

        Returns
        -------
        census : 1D-array or list of 1D-array
            The census for the population.
            census[i] is the probabiliy that a cell from that population belongs to cell set number i from the cell_set_matrix.
            List of censuses if a several populations were given as input, single census otherwise.

        Notes
        -----
        If several populations are given, they must all live in the same timepoint.
        """
        day = wot.model.unique_timepoint(*populations)
        all_ids_at_t = self.matrix.row_meta.index[self.matrix.row_meta['day'] == day]
        inter_ids = cell_set_matrix.row_meta.index.intersection(all_ids_at_t)
        if len(inter_ids) == 0:
            census = [ [0] * cell_set_matrix.x.shape[1] ] * len(populations)
        else:
            pop_indexer = all_ids_at_t.get_indexer_for(inter_ids)
            csm_indexer = cell_set_matrix.row_meta.index.get_indexer_for(inter_ids)
            def get_census(p):
                return np.dot(p[pop_indexer], cell_set_matrix.x[csm_indexer,:])
            norm = lambda p : p if np.isclose(np.sum(p), 0) else p / np.sum(p)
            census = np.asarray([get_census(norm(pop.p)) for pop in populations],
                    dtype=np.float64)

        if len(census) == 1:
            return census[0]
        else:
            return census

    def population_mean_and_variance(self, *populations):
        """
        Get the mean and variance of each gene for a population

        Parameters
        ----------
        *populations : wot.Population or list of wot.Population
            The population to be considered

        Returns
        -------
        mean : 1D-array or list of 1D-array
            The mean of each gene for the population.
            List of means if several populations were given as input, 1-D array otherwise.
        variance : 1D-array or list of 1D-array
            The variance of each gene for the population.
            List of variances if several populations were given as input, 1-D array otherwise.

        Notes
        -----
        If several populations are given, they must all live in the same timepoint.
        """
        day = wot.model.unique_timepoint(*populations)
        all_ids_at_t = self.matrix.row_meta.index[self.matrix.row_meta['day'] == day]
        all_indices_at_t = self.matrix.row_meta.index.get_indexer_for(all_ids_at_t)
        values = self.matrix.x[all_indices_at_t,:]
        means, variances = [], []
        for pop in populations:
            mean = np.average(values, weights=pop.p, axis=0)
            var  = np.average((values - mean) ** 2, weights=pop.p, axis=0)
            means.append(mean); variances.append(var)

        if len(means) == 1:
            return means[0],  variances[0]
        else:
            return means, variances

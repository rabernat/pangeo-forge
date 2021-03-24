"""
A Pangeo Forge Recipe
"""

import json
import logging
from abc import ABC, abstractmethod
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Hashable, Iterable, List, Optional

import fsspec
import fsspec.core
import xarray as xr
import zarr
from rechunker.types import MultiStagePipeline, ParallelPipelines, Stage

from .patterns import ExplicitURLSequence, VariableSequencePattern
from .storage import AbstractTarget, UninitializedTarget
from .utils import (
    calc_chunk_conflicts,
    chunked_iterable,
    fix_scalar_attr_encoding,
    lock_for_conflicts,
)

# use this filename to store global recipe metadata in the metadata_cache
# it will be written once (by prepare_target) and read many times (by store_chunk)
_GLOBAL_METADATA_KEY = "pangeo-forge-recipe-metadata.json"

logger = logging.getLogger(__name__)

# How to manually execute a recipe: ###
#
#   t = PangeoForgeTarget()
#   r = MyRecipe(target=t, **opts) # 1
#   # manual execution of recipe
#   for input_key in r.iter_inputs():
#       r.cache_input(input_key) # 4
#   r.prepare_target() # 3
#   for chunk_key in r.iter_chunks():
#       r.store_chunk(chunk_key) # 5
#   r.finalize_target() # 6


# 1) Initialize the Recipe object
# 2) Point the Recipe at its Target
# 3) Initialize the recipe.
#    Check if the target exists; if not, create it.
# 4) cache the inputs to proximate storage (OPTIONAL)
#    Some recipes won't need this (e.g. cloud to cloud)
#    If so, iter_inputs is just an empty iterator
# 5) Load each chunk from the inputs and store it in the target
#    Might be coming from the cache or might be read directly.
# 6)


class BaseRecipe(ABC):
    """Base recipe class from which all other Recipes inherit."""

    @property
    @abstractmethod
    def prepare_target(self) -> Callable[[], None]:
        """Prepare the recipe for execution by initializing the target.
        Attribute that returns a callable function.
        """
        pass

    @abstractmethod
    def iter_inputs(self) -> Iterable[Hashable]:
        """Iterate over all inputs."""
        pass

    @property
    @abstractmethod
    def cache_input(self) -> Callable[[Hashable], None]:
        """Copy an input from its source location to the cache.
        Attribute that returns a callable function.
        """
        pass

    @abstractmethod
    def iter_chunks(self) -> Iterable[Hashable]:
        """Iterate over all target chunks."""
        pass

    @property
    @abstractmethod
    def store_chunk(self) -> Callable[[Hashable], None]:
        """Store a chunk of data in the target.
        Attribute that returns a callable function.
        """
        pass

    @property
    @abstractmethod
    def finalize_target(self) -> Callable[[], None]:
        """Final step to finish the recipe after data has been written.
        Attribute that returns a callable function.
        """
        pass

    def to_pipelines(self) -> ParallelPipelines:
        """Translate recipe to pipeline for execution."""

        pipeline = []  # type: MultiStagePipeline
        if getattr(self, "cache_inputs", False):  # TODO: formalize this contract
            pipeline.append(Stage(self.cache_input, list(self.iter_inputs())))
        pipeline.append(Stage(self.prepare_target))
        pipeline.append(Stage(self.store_chunk, list(self.iter_chunks())))
        pipeline.append(Stage(self.finalize_target))
        pipelines = []  # type: ParallelPipelines
        pipelines.append(pipeline)
        return pipelines

    # https://stackoverflow.com/questions/59986413/achieving-multiple-inheritance-using-python-dataclasses
    def __post_init__(self):
        # just intercept the __post_init__ calls so they
        # aren't relayed to `object`
        pass


def _encode_key(key) -> str:
    return "-".join([str(k) for k in key])


def _input_metadata_fname(input_key):
    return "input-meta-" + _encode_key(input_key) + ".json"


def _chunk_metadata_fname(chunk_key) -> str:
    return "chunk-meta-" + _encode_key(chunk_key) + ".json"


# Notes about dataclasses:
# - https://www.python.org/dev/peps/pep-0557/#inheritance
# - https://stackoverflow.com/questions/51575931/class-inheritance-in-python-3-7-dataclasses
# The main awkward thing here is that, because we are using multiple inheritance
# with dataclasses, _ALL_ fields must have default values. This makes it impossible
# to have "required" keyword arguments--everything needs some default.
# That's whay we end up with `UninitializedTarget` and `_variable_sequence_pattern_default_factory`


@dataclass
class NetCDFtoZarrRecipe(BaseRecipe):
    """This class represents a dataset composed of many individual NetCDF files.
    This class uses Xarray to read and write data and writes its output to Zarr.
    The files are assumed to be arranged in a sequence along dimension ``sequence_dim``
    (commonly "time".)

    This class should not be used on its own but rather via one of its sub-classes.

    :param sequence_dim: The dimension name along which the inputs will be concatenated.
    :param inputs_per_chunk: The number of inputs to use in each chunk.
    :param nitems_per_input: The length of each input along the ``sequence_dim`` dimension.
      If ``None``, each input will be scanned and its metadata cached in order to
      determine the size of the target dataset.
    :param target: A location in which to put the dataset. Can also be assigned at run time.
    :param input_cache: A location in which to cache temporary data.
    :param metadata_cache: A location in which to cache metadata for inputs and chunks.
      Required if ``nitems_per_input=None``.
    :param cache_inputs: Whether to allow opening inputs directly which have not
      yet been cached. This could lead to very unstanble behavior if the inputs
      live behind a slow network connection.
    :param copy_input_to_local_file: Whether to copy the inputs to a temporary
      local file. In this case, a path (rather than file object) is passed to
      ``xr.open_dataset``. This is required for engines that can't open
      file-like objects (e.g. pynio).
    :param consolidate_zarr: Whether to consolidate the resulting Zarr dataset.
    :param xarray_open_kwargs: Extra options for opening the inputs with Xarray.
    :param xarray_concat_kwargs: Extra options to pass to Xarray when concatenating
      the inputs to form a chunk.
    :param delete_input_encoding: Whether to remove Xarray encoding from variables
      in the input dataset
    :param fsspec_open_kwargs: Extra options for opening the inputs with fsspec.
    :param process_input: Function to call on each opened input, with signature
      `(ds: xr.Dataset, filename: str) -> ds: xr.Dataset`.
    :param process_chunk: Function to call on each concatenated chunk, with signature
      `(ds: xr.Dataset) -> ds: xr.Dataset`.
    :param target_chunks: Desired chunk structure for the targret dataset.
    """

    sequence_dim: Optional[str] = None
    inputs_per_chunk: int = 1
    nitems_per_input: Optional[int] = 1
    target: AbstractTarget = field(default_factory=UninitializedTarget)
    input_cache: AbstractTarget = field(default_factory=UninitializedTarget)
    metadata_cache: AbstractTarget = field(default_factory=UninitializedTarget)
    cache_inputs: bool = True
    copy_input_to_local_file: bool = False
    consolidate_zarr: bool = True
    xarray_open_kwargs: dict = field(default_factory=dict)
    xarray_concat_kwargs: dict = field(default_factory=dict)
    delete_input_encoding: bool = True
    fsspec_open_kwargs: dict = field(default_factory=dict)
    process_input: Optional[Callable[[xr.Dataset, str], xr.Dataset]] = None
    process_chunk: Optional[Callable[[xr.Dataset], xr.Dataset]] = None
    target_chunks: Dict[str, int] = field(default_factory=dict)

    # private attributes, needed for type hints, not meant to be seen or accessed by user
    _sequence_dim_chunks: List[int] = field(default_factory=list, repr=False, init=False)
    _inputs: Dict[Hashable, str] = field(default_factory=dict, repr=False, init=False)

    def __post_init__(self):
        self._cache_metadata = self.nitems_per_input is None
        target_sequence_dim_chunks = self.target_chunks.get(self.sequence_dim)
        if (self.nitems_per_input is None) and (target_sequence_dim_chunks is None):
            raise ValueError(
                "Unable to determine target chunks. Please specify either "
                "`target_chunks` or `nitems_per_input`"
            )
        elif target_sequence_dim_chunks:
            self._sequence_dim_chunks = target_sequence_dim_chunks
        else:
            self._sequence_dim_chunks = self.nitems_per_input * self.inputs_per_chunk

        # TODO: more input validation

    @property
    def prepare_target(self) -> Callable:
        def _prepare_target():

            try:
                ds = self.open_target()
                logger.info("Found an existing dataset in target")
                logger.debug(f"{ds}")

                if self.target_chunks:
                    # TODO: check that target_chunks id compatibile with the
                    # existing chunks
                    pass

            except (FileNotFoundError, IOError, zarr.errors.GroupNotFoundError):
                logger.info("Creating a new dataset in target")
                # need to rewrite this as an append loop
                for chunk_key in self._init_chunks:
                    with self.open_chunk(chunk_key) as ds:
                        # need to have the data in memory to avoid weird chunk problems
                        ds.load()

                        # https://github.com/pydata/xarray/blob/5287c7b2546fc8848f539bb5ee66bb8d91d8496f/xarray/core/variable.py#L1069
                        for v in ds.variables:
                            if self.target_chunks:
                                this_var = ds[v]
                                chunks = {
                                    this_var.get_axis_num(dim): chunk
                                    for dim, chunk in self.target_chunks.items()
                                    if dim in this_var.dims
                                }

                                chunks = tuple(
                                    chunks.get(n, s) for n, s in enumerate(this_var.shape)
                                )
                                ds[v].encoding["chunks"] = chunks
                            else:
                                ds[v].encoding["chunks"] = ds[v].shape

                        target_mapper = self.target.get_mapper()
                        ds.to_zarr(target_mapper, mode="a", compute=False)

            # Regardless of whether there is an existing dataset or we are creating a new one,
            # we need to expand the sequence_dim to hold the entire expected size of the data
            input_sequence_lens = self.sequence_lens()
            n_sequence = sum(input_sequence_lens)
            self.expand_target_dim(self.sequence_dim, n_sequence)

            if not self.nitems_per_input:
                # if nitems_per_input is not constant, we need to cache this info
                recipe_meta = {"input_sequence_lens": input_sequence_lens}
                meta_mapper = self.metadata_cache.get_mapper()
                # we are saving a dictionary with one key (input_sequence_lens)
                meta_mapper[_GLOBAL_METADATA_KEY] = json.dumps(recipe_meta).encode("utf-8")

        return _prepare_target

    @property
    def store_chunk(self) -> Callable:
        def _store_chunk(chunk_key):
            with self.open_chunk(chunk_key) as ds_chunk:
                # writing a region means that all the variables MUST have sequence_dim
                to_drop = [
                    v for v in ds_chunk.variables if self.sequence_dim not in ds_chunk[v].dims
                ]
                ds_chunk = ds_chunk.drop_vars(to_drop)
                target_mapper = self.target.get_mapper()
                write_region, conflicts = self.region_and_conflicts_for_chunk(chunk_key)
                with lock_for_conflicts(conflicts):
                    logger.info(f"Storing chunk '{chunk_key}' to Zarr region {write_region}")
                    ds_chunk.to_zarr(target_mapper, region=write_region)

        return _store_chunk

    @property
    def finalize_target(self) -> Callable:
        def _finalize_target():
            if self.consolidate_zarr:
                logger.info("Consolidating Zarr metadata")
                target_mapper = self.target.get_mapper()
                zarr.consolidate_metadata(target_mapper)

        return _finalize_target

    @contextmanager
    def input_opener(self, fname: str):
        if self.copy_input_to_local_file and not self.cache_input:
            raise ValueError
        fs = fsspec.core.url_to_fs(fname, **self.fsspec_open_kwargs)
        if self.cache_input:
            fs = fsspec.filesystem("simplecache", fs=fs, cache_storage=self.input_cache)
        with fs.open(fname, "rb") as fp:
            if self.copy_input_to_local_file:
                yield fp.name
            else:
                yield fp

    @contextmanager
    def open_input(self, input_key: Hashable):
        fname = self._inputs[input_key]
        logger.info(f"Opening input with Xarray {input_key}: '{fname}'")
        with self.input_opener(fname) as f:
            logger.info(f"f is {f}")
            ds = xr.open_dataset(f, **self.xarray_open_kwargs)
            # Explicitly load into memory;
            # if we don't do this, we get a ValueError: seek of closed file.
            # But there will be some cases where we really don't want to load.
            # how to keep around the open file object?
            # ds = ds.load()

            ds = fix_scalar_attr_encoding(ds)

            if self.delete_input_encoding:
                for var in ds.variables:
                    ds[var].encoding = {}

            if self.process_input is not None:
                ds = self.process_input(ds, str(fname))

            logger.debug(f"{ds}")
            yield ds

    def cache_input_metadata(self, input_key: Hashable):
        logger.info(f"Caching metadata for input '{input_key}'")
        with self.open_input(input_key) as ds:
            metadata = ds.to_dict(data=False)
            mapper = self.metadata_cache.get_mapper()
            mapper[_input_metadata_fname(input_key)] = json.dumps(metadata).encode("utf-8")

    @contextmanager
    def open_chunk(self, chunk_key):
        logger.info(f"Concatenating inputs for chunk '{chunk_key}'")
        inputs = self.inputs_for_chunk(chunk_key)

        # need to open an unknown number of contexts at the same time
        with ExitStack() as stack:
            dsets = [stack.enter_context(self.open_input(i)) for i in inputs]
            # explicitly chunking prevents eager evaluation during concat
            # dsets = [ds.chunk() for ds in dsets]
            # but that leads to corrupted data!

            # CONCAT DELETES ENCODING!!!
            # OR NO IT DOESN'T! Not in the latest version of xarray?
            ds = xr.concat(dsets, self.sequence_dim, **self.xarray_concat_kwargs)

            if self.process_chunk is not None:
                ds = self.process_chunk(ds)

            logger.debug(f"{ds}")

            # TODO: maybe do some chunking here?
            yield ds

    def open_target(self):
        target_mapper = self.target.get_mapper()
        return xr.open_zarr(target_mapper)

    def expand_target_dim(self, dim, dimsize):
        target_mapper = self.target.get_mapper()
        zgroup = zarr.open_group(target_mapper)

        ds = self.open_target()
        sequence_axes = {v: ds[v].get_axis_num(dim) for v in ds.variables if dim in ds[v].dims}

        for v, axis in sequence_axes.items():
            arr = zgroup[v]
            shape = list(arr.shape)
            shape[axis] = dimsize
            arr.resize(shape)

        # now explicity write the sequence coordinate to avoid missing data
        # when reopening
        if dim in zgroup:
            zgroup[dim][:] = 0

    def inputs_for_chunk(self, chunk_key):
        return self._chunks_inputs[chunk_key]

    def iter_inputs(self):
        for chunk_key in self.iter_chunks():
            for input in self.inputs_for_chunk(chunk_key):
                yield input

    def region_and_conflicts_for_chunk(self, chunk_key):
        # return a dict suitable to pass to xr.to_zarr(region=...)
        # specifies where in the overall array to put this chunk's data
        # also return the conflicts with other chunks

        input_keys = self.inputs_for_chunk(chunk_key)
        if self.nitems_per_input:
            stride = self.nitems_per_input * self.inputs_per_chunk
            start = self.chunk_position(chunk_key) * stride
            stop = start + stride
            input_sequence_lens = (self.nitems_per_input,) * self._n_inputs_along_sequence
        else:
            input_sequence_lens = json.loads(
                self.metadata_cache.get_mapper()[_GLOBAL_METADATA_KEY]
            )["input_sequence_lens"]
            start = sum(input_sequence_lens[: self.input_position(input_keys[0])])
            chunk_len = sum([input_sequence_lens[self.input_position(k)] for k in input_keys])
            stop = start + chunk_len

        all_chunk_conflicts = calc_chunk_conflicts(input_sequence_lens, self._sequence_dim_chunks)
        this_chunk_conflicts = [all_chunk_conflicts[self.input_position(k)] for k in input_keys]

        region_slice = slice(start, stop)
        return {self.sequence_dim: region_slice}, this_chunk_conflicts

    def iter_chunks(self):
        for k in self._chunks_inputs:
            yield k

    def get_input_meta(self, *input_keys) -> Dict:
        meta_mapper = self.metadata_cache.get_mapper()
        # getitems should be async; much faster than serial calls
        all_meta_raw = meta_mapper.getitems([_input_metadata_fname(k) for k in input_keys])
        return {k: json.loads(raw_bytes) for k, raw_bytes in all_meta_raw.items()}

    def input_sequence_lens(self, *input_keys) -> List[int]:
        if self.nitems_per_input:
            return list((self.nitems_per_input,) * len(input_keys))  # struggling to please flake8
        # read per-input metadata; this is distinct from global metadata
        input_meta = self.get_input_meta(*input_keys)
        return [m["dims"][self.sequence_dim] for m in input_meta.values()]


@dataclass
class NetCDFtoZarrSequentialRecipe(NetCDFtoZarrRecipe):
    """There is only one sequence of input files. Each file can contain
    many variables.

    :param input_urls: The inputs used to generate the dataset.
    """

    input_urls: Iterable[str] = field(repr=False, default_factory=list)

    def __post_init__(self):
        super().__post_init__()
        input_pattern = ExplicitURLSequence(self.input_urls)
        self._inputs = {k: v for k, v in input_pattern}
        self._chunks_inputs = {
            (k,): v for k, v in enumerate(chunked_iterable(self._inputs, self.inputs_per_chunk))
        }
        self._n_inputs_along_sequence = len(self._inputs)

        # just the first chunk is needed to initialize the recipe
        self._init_chunks = [next(iter(self._chunks_inputs))]

    def input_position(self, input_key):
        return input_key[0]

    def chunk_position(self, chunk_key):
        return chunk_key[0]

    def sequence_lens(self):
        return self.input_sequence_lens(*self.iter_inputs())


def _variable_sequence_pattern_default_factory() -> VariableSequencePattern:
    return VariableSequencePattern(fmt_string="", keys={"variable": [], "_null": []})


@dataclass
class NetCDFtoZarrMultiVarSequentialRecipe(NetCDFtoZarrRecipe):
    """There are muliples sequences of input files (but all along the same dimension.)
    Different variables live in different files.

    :param input_pattern: An pattern used to generate the input file names.
    """

    input_pattern: VariableSequencePattern = field(
        default_factory=_variable_sequence_pattern_default_factory
    )

    def __post_init__(self):
        super().__post_init__()
        self._variables = self.input_pattern.keys["variable"]
        self._other_key = [k for k in self.input_pattern.keys if k != "variable"][0]
        self._inputs = {k: v for k, v in self.input_pattern}
        # input keys are tuples like
        #  ("temp", 0)
        #  ("temp", 1)
        #  ...
        #  ("salt", n)
        # chunk_keys will be similarly named, but the index will have a different meaning

        chunks_inputs = {}
        # generate this iteratively
        for vname in self._variables:
            # a hacky way to filter
            sequence_keys = [k[1] for k in self._inputs if k[0] == vname]
            for chunk_key, chunk_inputs in enumerate(
                chunked_iterable(sequence_keys, self.inputs_per_chunk)
            ):
                chunks_inputs[(vname, chunk_key)] = [
                    (vname, input_sequence_key) for input_sequence_key in chunk_inputs
                ]
        self._chunks_inputs = chunks_inputs
        self._n_inputs_along_sequence = len(self.input_pattern.keys[self._other_key])

        # should be the first chunk from each variable
        self._init_chunks = [chunk_key for chunk_key in self._chunks_inputs if chunk_key[1] == 0]

    def input_position(self, input_key):
        return self.input_pattern.keys[self._other_key].index(input_key[1])

    def chunk_position(self, chunk_key):
        return chunk_key[1]

    def sequence_lens(self):
        variables = self.input_pattern.keys["variable"]
        lens_by_variable = {}
        for v in variables:
            variable_input_keys = [k for k in self.iter_inputs() if k[0] == v]
            lens_by_variable[v] = self.input_sequence_lens(*variable_input_keys)
        if not all([lens_by_variable[v] == lens_by_variable[variables[0]]]):
            raise ValueError(f"Inconsistent sequence_len found for variables. f{lens_by_variable}")

        return lens_by_variable[variables[0]]

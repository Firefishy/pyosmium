# SPDX-License-Identifier: BSD-2-Clause
#
# This file is part of pyosmium. (https://osmcode.org/pyosmium/)
#
# Copyright (C) 2024 Sarah Hoffmann <lonvia@denofr.de> and others.
# For a full list of authors see the git log.
from typing import Iterable, Tuple, Any
from pathlib import Path

import osmium

class FileProcessor:
    """ A generator that emits OSM objects read from a file.
    """

    def __init__(self, filename, entities=osmium.osm.ALL):
        if isinstance(filename, (osmium.io.File, osmium.io.FileBuffer)):
            self._file = filename
        elif isinstance(filename, (str, Path)):
            self._file = osmium.io.File(str(filename))
        else:
            raise TypeError("File must be an osmium.io.File, osmium.io.FileBuffer, str or Path")
        self._reader = osmium.io.Reader(self._file, entities)
        self._entities = entities
        self._node_store = None
        self._area_handler = None
        self._filters = []

    @property
    def header(self):
        """ Return the header information for the file to be read.
        """
        return self._reader.header()

    @property
    def node_location_storage(self):
        """ Return the node location cache, if enabled.
            This can be used to manually look up locations of nodes.
        """
        return self._node_store

    def with_locations(self, storage='flex_mem'):
        """ Enable caching of node locations. This is necessary in order
            to get geometries for ways and relations.
        """
        if not (self._entities & osmium.osm.NODE):
            raise RuntimeError('Nodes not read from file. Cannot enable location cache.')
        if isinstance(storage, str):
            self._node_store = osmium.index.create_map(storage)
        elif storage is None or isinstance(storage, osmium.index.LocationTable):
            self._node_store = storage
        else:
            raise TypeError("'storage' argument must be a LocationTable or a string describing the index")

        return self

    def with_areas(self):
        """ Enable area processing. When enabled, then closed ways and
            relations of type multipolygon will also be returned as an
            Area type.

            Automatically enables location caching, if it was not yet set.
            It uses the default location cache type. To use a different
            cache tyoe, you need to call with_locations() explicity.

            Area processing requires that the file is read twice. This
            happens transparently.
        """
        if self._area_handler is None:
            self._area_handler = osmium.area.AreaManager()
            if self._node_store is None:
                self.with_locations()
        return self

    def with_filter(self, filt):
        """ Add a filter function that is called before an object is
            returned in the iterator.
        """
        self._filters.append(filt)
        return self

    def __iter__(self):
        """ Return the iterator over the file.
        """
        handlers = []

        if self._node_store is not None:
            lh = osmium.NodeLocationsForWays(self._node_store)
            lh.ignore_errors()
            handlers.append(lh)

        if self._area_handler is None:
            yield from osmium.OsmFileIterator(self._reader, *handlers, *self._filters)
            return

        # need areas, do two pass handling
        rd = osmium.io.Reader(self._file, osmium.osm.RELATION)
        try:
            osmium.apply(rd, *self._filters, self._area_handler.first_pass_handler())
        finally:
            rd.close()

        buffer_it = osmium.BufferIterator(*self._filters)
        handlers.append(self._area_handler.second_pass_to_buffer(buffer_it))

        for obj in osmium.OsmFileIterator(self._reader, *handlers, *self._filters):
            yield obj
            if buffer_it:
                yield from buffer_it

        # catch anything after the final flush
        if buffer_it:
            yield from buffer_it


def zip_processors(*procs: FileProcessor) -> Iterable[Tuple[Any, ...]]:
    """ Return the data from the FileProcessors in parallel such
        that objects with the same ID are returned at the same time.

        The processors must contain sorted data or the results are
        undefined.
    """
    TID = {'n': 0, 'w': 1, 'r': 2, 'a': 3, 'c': 4}

    class _CompIter:

        def __init__(self, fp):
            self.iter = iter(fp)
            self.current = None
            self.comp = None

        def val(self, nextid):
            """ Return current object if it corresponds to the given object ID.
            """
            if self.comp == nextid:
                return self.current
            return None

        def next(self, nextid):
            """ Get the next object ID, if and only if nextid points to the
                previously returned object ID. Otherwise return the previous
                ID again.
            """
            if self.comp == nextid:
                self.current = next(self.iter, None)
                if self.current is None:
                    self.comp = (100, 0) # end of file marker. larger than any ID
                else:
                    self.comp = (TID[self.current.type_str()], self.current.id)
            return self.comp


    iters = [_CompIter(p) for p in procs]

    nextid = min(i.next(None) for i in iters)

    while nextid[0] < 100:
        yield (i.val(nextid) for i in iters)

        nextid = min(i.next(nextid) for i in iters)

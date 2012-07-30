# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""
Memcached backend wrapper for syncstorage.

This module implements a memcached layer for the SyncStorage backend API.
It caches frequently-used metadata in memcache while passing the bulk of
the operations on to an underlying backend implementation.  It is also capable
of storing entire collections in memcache without hitting the backend.

The following memcached keys are used:

    * userid:metadata         metadata about the storage and collections
    * userid:c:<collection>   cached data for a particular collection

A key prefix can also be defined to avoid clobbering unrelated data in a
shared memcached setup.  It defaults to the empty string.

The "metadata" key contains a JSON object describing the state of the store.
The data is all stored as a single key so that it can be updated atomically.
It has the following structure:

    {
      "size":               <approximate total size of the stored data>,
      "last_size_recalc":   <time when size was last recalculated>,
      "modified":           <last-modified timestamp for the entire storage>,
      "collections": {
         <collection name>:  <last-modified timestamp for the collection>,
      },
    }

For each collection to be stored in memcache, the corresponding key contains
a JSON mapping from item ids to BSO objects along with a record of the last-
modified timestamp for that collection:

    {
      "modified":   <last-modified timestamp for the collection>,
      "items": {
        <item id>:  <BSO object for that item>,
      }
    }

To avoid the cached data getting out of sync with the underlying storage, we
explicitly mark the cache as dirty before performing any write operations.
In the unlikely event of a mid-operation crash, we'll notice the dirty cache
and fall back to the underlying store instead of using potentially inconsistent
data from memcache.
"""

import time
import threading
import contextlib

from syncstorage.util import get_timestamp, from_timestamp
from syncstorage.storage import (SyncStorage,
                                 StorageError,
                                 ConflictError,
                                 CollectionNotFoundError,
                                 ItemNotFoundError)

from mozsvc.storage.mcclient import MemcachedClient


# Recalculate quota at most once per hour.
SIZE_RECALCULATION_PERIOD = 60 * 60

# Expire cache-based lock after five minutes.
DEFAULT_CACHE_LOCK_TTL = 5 * 60


def _key(*names):
    return ":".join(map(str, names))


def _as_list(item_or_list):
    """Coerce value from config file into a list.

    This is a little helper that can be used anywhere you expect a list of
    items, but may be given just a single item.  It converts said single
    item into a list.
    """
    if isinstance(item_or_list, (list, tuple)):
        return item_or_list
    return [item_or_list]


class MemcachedStorage(SyncStorage):
    """Memcached caching wrapper for SyncStorage backends.

    The SyncStorage implementation wraps another storage backend to provide
    a caching layer.  You may specify the following arguments:

        * storage:  the underlying SyncStorage object that is to be wrapped.a
        * cache_servers:  a list of memcached server URLs.
        * cached_collections:  a list of names of collections that should
                               be duplicated into memcache for fast access.
        * cache_only_collections:  a list of names of collections that should
                                   be stored *only* in memcached, and never
                                   written through to the bacend.
        * cache_key_prefix:  a string to be prepended to all memcached keys,
                             useful for namespacing in shared cache setups.
        * cache_pool_size:  the maximum number of active memcache clients.
        * cache_pool_timeout:  the maximum lifetime of each memcache client.

    """

    def __init__(self, storage, cache_servers=None, cache_key_prefix="",
                 cache_pool_size=None, cache_pool_timeout=60,
                 cached_collections=(), cache_only_collections=(),
                 cache_lock=False, cache_lock_ttl=None, **kwds):
        self.storage = storage
        self.cache = MemcachedClient(cache_servers, cache_key_prefix,
                                     cache_pool_size, cache_pool_timeout)
        self.cached_collections = {}
        for collection in _as_list(cached_collections):
            colmgr = CachedManager(self, collection)
            self.cached_collections[collection] = colmgr
        self.cache_only_collections = {}
        for collection in _as_list(cache_only_collections):
            colmgr = CacheOnlyManager(self, collection)
            self.cache_only_collections[collection] = colmgr
        self.cache_lock = cache_lock
        if cache_lock_ttl is None:
            self.cache_lock_ttl = DEFAULT_CACHE_LOCK_TTL
        else:
            self.cache_lock_ttl = cache_lock_ttl
        # Keep a threadlocal to track the currently-held locks.
        # This is needed to make the read locking API reentrant.
        self._tldata = threading.local()

    def _iter_cache_keys(self, userid):
        """Iterator over all potential cache keys for the given userid.

        This method yields all potential cacher keys for the given userid,
        including their metadata key and the keys for any cached collections.
        The yielded keys do *not* include the key prefix, if any.
        """
        yield _key(userid, "metadata")
        for colmgr in self.cached_collections.itervalues():
            yield colmgr.get_key(userid)
        for colmgr in self.cache_only_collections.itervalues():
            yield colmgr.get_key(userid)

    def _get_collection_manager(self, collection):
        """Get a collection-management object for the named collection.

        This class delegates all collection-level operations to a "collection
        manager" object.  The manager for a given collection will be different
        depending on the required caching characteristics, and this method
        gets and returns on appropriate manager for the named collection.
        """
        try:
            return self.cached_collections[collection]
        except KeyError:
            try:
                return self.cache_only_collections[collection]
            except KeyError:
                return UncachedManager(self, collection)

    #
    # APIs for collection-level locking.
    #
    # This class provides the option of locking at the memcache level rather
    # than calling through to the underlying storage engine.  Such locks
    # are just simple mutex keys in memcache, one per collection.  If you
    # can successfully add the key then you get the lock, if it already
    # exists then someone else holds the lock.  If you crash while holding
    # the lock, it will eventually expire.
    #

    @contextlib.contextmanager
    def lock_for_read(self, userid, collection):
        """Acquire a shared read lock on the named collection."""
        # We need to be able to take a read lock in some internal methods,
        # so that we can populate the cache with consistent data.
        # Use a thread-local set of held locks to make it reentrant.
        try:
            read_locks = self._tldata.read_locks
        except AttributeError:
            read_locks = self._tldata.read_locks = set()
        # If we already have a read lock, don't take it again.
        if (userid, collection) in read_locks:
            yield None
            return
        # Otherwise take the lock and mark it as being held.
        if self.cache_lock or collection in self.cache_only_collections:
            lock = self._lock_in_memcache(userid, collection)
        else:
            lock = self.storage.lock_for_read(userid, collection)
        with lock:
            read_locks.add((userid, collection))
            try:
                yield None
            finally:
                read_locks.remove((userid, collection))

    def lock_for_write(self, userid, collection):
        """Acquire an exclusive write lock on the named collection."""
        if self.cache_lock or collection in self.cache_only_collections:
            return self._lock_in_memcache(userid, collection)
        else:
            return self.storage.lock_for_write(userid, collection)

    @contextlib.contextmanager
    def _lock_in_memcache(self, userid, collection):
        """Helper method to take a memcache-level lock on a collection."""
        ttl = self.cache_lock_ttl
        now = time.time()
        key = _key(userid, "lock", collection)
        if not self.cache.add(key, True, time=ttl):
            raise ConflictError
        try:
            yield None
        finally:
            if time.time() - now >= ttl:
                msg = "Lock expired while we were holding it"
                raise RuntimeError(msg)
            self.cache.delete(key)

    #
    # APIs to operate on the entire storage.
    #

    def get_storage_timestamp(self, userid):
        """Returns the last-modified timestamp for the entire storage."""
        # Try to use the cached value.
        ts = self._get_metadata(userid)["modified"]
        # Fall back to live data if it's dirty.
        if ts is None:
            ts = self.storage.get_storage_timestamp(userid)
            for colmgr in self.cache_only_collections.itervalues():
                ts = max(ts, colmgr.get_timestamp(userid))
        return ts

    def get_collection_timestamps(self, userid):
        """Returns the collection timestamps for a user."""
        # Try to use the cached value.
        stamps = self._get_metadata(userid)["collections"]
        # Fall back to live data for any collections that are dirty.
        for collection, ts in stamps.items():
            if ts is None:
                colmgr = self._get_collection_manager(collection)
                try:
                    stamps[collection] = colmgr.get_timestamp(userid)
                except CollectionNotFoundError:
                    del stamps[collection]
        return stamps

    def get_collection_counts(self, userid):
        """Returns the collection counts."""
        # Read most of the data from the database.
        counts = self.storage.get_collection_counts(userid)
        # Add in counts for collections stored only in memcache.
        for colmgr in self.cache_only_collections.itervalues():
            try:
                items = colmgr.get_items(userid)
            except CollectionNotFoundError:
                pass
            else:
                counts[colmgr.collection] = len(items)
        return counts

    def get_collection_sizes(self, userid):
        """Returns the total size for each collection."""
        # Read most of the data from the database.
        sizes = self.storage.get_collection_sizes(userid)
        # Add in sizes for collections stored only in memcache.
        for colmgr in self.cache_only_collections.itervalues():
            try:
                items = colmgr.get_items(userid)
                payloads = (item.get("payload", "") for item in items)
                sizes[colmgr.collection] = sum(len(p) for p in payloads)
            except CollectionNotFoundError:
                pass
        # Since we've just gone to the trouble of recalculating sizes,
        # we might as well update the cached total size as well.
        self._update_total_size(userid, sum(sizes.itervalues()))
        return sizes

    def get_total_size(self, userid, recalculate=False):
        """Returns the total size of a user's storage data."""
        return self._get_metadata(userid, recalculate)["size"]

    def delete_storage(self, userid):
        """Removes all data for the user."""
        for key in self._iter_cache_keys(userid):
            self.cache.delete(key)
        self.storage.delete_storage(userid)

    #
    # APIs to operate on an individual collection
    #

    def get_collection_timestamp(self, userid, collection):
        """Returns the last-modified timestamp for the named collection."""
        # It's likely cheaper to read all cached stamps out of memcache
        # than to read just the single timestamp from the database.
        stamps = self.get_collection_timestamps(userid)
        try:
            ts = stamps[collection]
        except KeyError:
            raise CollectionNotFoundError
        # Refresh from the live data if dirty.
        if ts is None:
            colmgr = self._get_collection_manager(collection)
            ts = colmgr.get_timestamp(userid)
        return ts

    def get_items(self, userid, collection, **kwds):
        """Returns items from a collection"""
        colmgr = self._get_collection_manager(collection)
        return colmgr.get_items(userid, **kwds)

    def get_item_ids(self, userid, collection, **kwds):
        """Returns item idss from a collection"""
        colmgr = self._get_collection_manager(collection)
        return colmgr.get_item_ids(userid, **kwds)

    def set_items(self, userid, collection, items):
        """Creates or updates multiple items in a collection."""
        colmgr = self._get_collection_manager(collection)
        with self._mark_collection_dirty(userid, collection) as update:
            ts = colmgr.set_items(userid, items)
            size = sum(len(item.get("payload", "")) for item in items)
            update(ts, ts, size)
            return ts

    def delete_collection(self, userid, collection):
        """Deletes an entire collection."""
        colmgr = self._get_collection_manager(collection)
        with self._mark_collection_dirty(userid, collection) as update:
            ts = colmgr.del_collection(userid)
            update(ts, None)
            return ts

    def delete_items(self, userid, collection, items):
        """Deletes multiple items from a collection."""
        colmgr = self._get_collection_manager(collection)
        with self._mark_collection_dirty(userid, collection) as update:
            ts = colmgr.del_items(userid, items)
            update(ts, ts)
            return ts

    #
    # Items APIs
    #

    def get_item_timestamp(self, userid, collection, item):
        """Returns the last-modified timestamp for the named item."""
        colmgr = self._get_collection_manager(collection)
        return colmgr.get_item_timestamp(userid, item)

    def get_item(self, userid, collection, item):
        """Returns one item from a collection."""
        colmgr = self._get_collection_manager(collection)
        return colmgr.get_item(userid, item)

    def set_item(self, userid, collection, item, data):
        """Creates or updates a single item in a collection."""
        colmgr = self._get_collection_manager(collection)
        with self._mark_collection_dirty(userid, collection) as update:
            res = colmgr.set_item(userid, item, data)
            size = len(data.get("payload", ""))
            update(res["modified"], res["modified"], size)
            return res

    def delete_item(self, userid, collection, item):
        """Deletes a single item from a collection."""
        colmgr = self._get_collection_manager(collection)
        with self._mark_collection_dirty(userid, collection) as update:
            ts = colmgr.del_item(userid, item)
            update(ts, ts)
            return ts

    #
    #  Private APIs for managing the cached metadata
    #

    def _get_metadata(self, userid, recalculate_size=False):
        """Get the metadata dict, recalculating things if necessary.

        This method pulls the dict of metadata out of memcache and returns it.
        If there is no information yet in memcache then it pulls the data from
        the underlying storage, cache it and then returns it.

        If recalculate_size is given and True, then the cache size value will
        be recalculated from the store if it is more than an hour old.
        """
        key = _key(userid, "metadata")
        data, casid = self.cache.gets(key)
        # If there is no cached metadata, initialize it from the storage.
        # Use CAS to avoid overwriting other changes, but don't error out if
        # the write fails - it just means that someone else beat us to it.
        if data is None:
            # Get the mapping of collection names to timestamps.
            # Make sure to include any cache-only collections.
            stamps = self.storage.get_collection_timestamps(userid)
            for colmgr in self.cached_collections.itervalues():
                if colmgr.collection not in stamps:
                    try:
                        ts = colmgr.get_timestamp(userid)
                        stamps[colmgr.collection] = ts
                    except CollectionNotFoundError:
                        pass
            # Get the storage-level modified time.
            # Make sure it's not less than any collection-level timestamps.
            modified = self.storage.get_storage_timestamp(userid)
            if stamps:
                modified = max(modified, max(stamps.itervalues()))
            # Calculate the total size if requested,
            # but don't bother if it's not necessary.
            if not recalculate_size:
                last_size_recalc = 0
                size = 0
            else:
                last_size_recalc = int(time.time())
                size = self._recalculate_total_size(userid)
            # Store it all back into the cache.
            data = {
                "size": size,
                "last_size_recalc": last_size_recalc,
                "modified": modified,
                "collections": stamps,
            }
            self.cache.cas(key, data, casid)
        # Recalculate the size if it appears to be out of date.
        # Use CAS to avoid clobbering changes but don't let it fail us.
        elif recalculate_size:
            recalc_period = time.time() - data["last_size_recalc"]
            if recalc_period > SIZE_RECALCULATION_PERIOD:
                data["last_size_recalc"] = int(time.time())
                data["size"] = self._recalculate_total_size(userid)
                self.cache.cas(key, data, casid)
        return data

    def _update_total_size(self, userid, size):
        """Update the cached value for total storage size."""
        key = _key(userid, "metadata")
        data, casid = self.cache.gets(key)
        if data is None:
            self._get_metadata(userid)
            data, casid = self.cache.gets(key)
        data["last_size_recalc"] = int(time.time())
        data["size"] = size
        self.cache.cas(key, data, casid)

    def _recalculate_total_size(self, userid):
        """Re-calculate total size from the database."""
        size = self.storage.get_total_size(userid)
        for colmgr in self.cache_only_collections.itervalues():
            try:
                items = colmgr.get_items(userid)
                payloads = (item.get("payload", "") for item in items)
                size += sum(len(p) for p in payloads)
            except CollectionNotFoundError:
                pass
        return size

    @contextlib.contextmanager
    def _mark_collection_dirty(self, userid, collection):
        """Context manager for marking collections as dirty during write.

        To prevent the cache from getting out of sync with the underlying store
        it is necessary to mark a collection as dirty before performing any
        modifications on it.  This is a handy context manager that can take
        care of that, as well as update the timestamps with new results when
        the modification is complete.

        The context object associated with this method is a callback function
        that can be used to update the stored metadata.  It accepts the top-
        level storage timestamp, collection-level timestamp, and a total size
        increment as its three arguments.  Example usage::

            with self._mark_collection_dirty(userid, collection) as update:
                colobj = self._get_collection_manager(collection)
                modified = colobj.set_item(userid, "test", {"payload": "TEST"})
                update(modified, modified, len("TEST"))

        """
        # Get the old values from the metadata.
        # We can't call _get_metadata directly because we also want the casid.
        key = _key(userid, "metadata")
        data, casid = self.cache.gets(key)
        if data is None:
            self._get_metadata(userid)
            data, casid = self.cache.gets(key)

        # Write None into the metadata to mark things as dirty.
        modified = data["modified"]
        col_modified = data["collections"].get(collection)
        data["modified"] = None
        data["collections"][collection] = None
        if not self.cache.cas(key, data, casid):
            raise ConflictError

        # Define the callback function for the calling code to use.
        # We also use this function internally to recover from errors.
        update_was_called = []

        def update(modified=modified, col_modified=col_modified, size_incr=0):
            assert not update_was_called
            update_was_called.append(True)
            data["modified"] = modified
            if col_modified is None:
                del data["collections"][collection]
            else:
                data["collections"][collection] = col_modified
            data["size"] += size_incr
            # We assume the write lock is held to avoid conflicting changes.
            # Sadly, using CAS again would return another round-trip.
            self.cache.set(key, data)

        # Yield out to the calling code.
        # It can call the yielded function to provide new metadata.
        # If they don't call it, then we cannot make any assumptions about
        # the consistency of the cached data and must leave things marked
        # as dirty until another write cleans it up.
        try:
            yield update
        except StorageError:
            # If a storage-related error occurs, then we know that the
            # operation wrapped by the calling code did not succeed.
            # It's therefore safe to roll back to previous values.
            if not update_was_called:
                update()
            raise


#  Collections stored in the MemcachedStorage class can have different
#  behaviours associated with them, depending on whether they are not
#  cached at all, cached in write-through mode, or cached without writing
#  back to the underlying store.  To simplify the code, we break out the
#  operations for each type of collection into a "manager" class.

class UncachedManager(object):
    """Manager class for collections that are not stored in memcache at all.

    This class provides methods for operating on a collection that is stored
    only in the backing store, not in memcache.  It just passes the method
    calls through, and exists only to simplify the main API by providing a
    common interface to all types of collection.
    """

    def __init__(self, owner, collection):
        self.owner = owner
        self.collection = collection

    def get_timestamp(self, userid):
        storage = self.owner.storage
        return storage.get_collection_timestamp(userid, self.collection)

    def get_items(self, userid, **kwds):
        storage = self.owner.storage
        return storage.get_items(userid, self.collection, **kwds)

    def get_item_ids(self, userid, **kwds):
        storage = self.owner.storage
        return storage.get_item_ids(userid, self.collection, **kwds)

    def set_items(self, userid, items):
        storage = self.owner.storage
        return storage.set_items(userid, self.collection, items)

    def del_collection(self, userid):
        storage = self.owner.storage
        return storage.delete_collection(userid, self.collection)

    def del_items(self, userid, items):
        storage = self.owner.storage
        return storage.delete_items(userid, self.collection, items)

    def get_item_timestamp(self, userid, item):
        storage = self.owner.storage
        return storage.get_item_timestamp(userid, self.collection, item)

    def get_item(self, userid, item):
        storage = self.owner.storage
        return storage.get_item(userid, self.collection, item)

    def set_item(self, userid, item, bso):
        storage = self.owner.storage
        return storage.set_item(userid, self.collection, item, bso)

    def del_item(self, userid, item):
        storage = self.owner.storage
        return storage.delete_item(userid, self.collection, item)


class _CachedManagerBase(object):
    """Common functionality for CachedManager and CacheOnlyManager.

    This class holds the duplicated logic between our two different types
    of in-cache collection managers: collections that are both in the cacha
    and in the backing store, and collections that exist solely in memcache.
    """

    def __init__(self, owner, collection):
        self.owner = owner
        self.collection = collection

    def get_key(self, userid):
        return _key(userid, "c", self.collection)

    @property
    def storage(self):
        return self.owner.storage

    @property
    def cache(self):
        return self.owner.cache

    #
    # Methods that need to be implemented by subclasses.
    # All the rest of the functionality is implemented in terms of these.
    #

    def get_cached_data(self, userid):
        raise NotImplementedError

    def set_items(self, userid, items):
        raise NotImplementedError

    def del_collection(self, userid):
        raise NotImplementedError

    def del_items(self, userid, items):
        raise NotImplementedError

    def set_item(self, userid, item, bso):
        raise NotImplementedError

    def del_item(self, userid, item):
        raise NotImplementedError

    #
    # Helper methods for updating cached collection data.
    # Subclasses use this common logic for updating the cache, but
    # need to layer different steps around it.
    #

    def _set_items(self, userid, items, modified, data, casid):
        """Update the cached data by setting the given items.

        This method performs the equivalent of SyncStorage.set_items() on
        the cached data.  You must provide the new modification timestamp,
        the existing data dict, and the casid of the data currently stored
        in memcache.

        It returns the number of items that were newly created, which may
        be less than the number of items given if some already existed in
        the cached data.
        """
        if not data:
            data = {"modified": modified, "items": {}}
        elif data["modified"] >= modified:
            raise ConflictError
        num_created = 0
        for bso in items:
            if "payload" in bso:
                bso["modified"] = modified
            try:
                data["items"][bso["id"]].update(bso)
            except KeyError:
                num_created += 1
                data["items"][bso["id"]] = bso
        if num_created > 0:
            data["modified"] = modified
        key = self.get_key(userid)
        if not self.cache.cas(key, data, casid):
            raise ConflictError
        return num_created

    def _del_items(self, userid, items, modified, data, casid):
        """Update the cached data by deleting the given items.

        This method performs the equivalent of SyncStorage.delete_items() on
        the cached data.  You must provide the new modification timestamp,
        the existing data dict, and the casid of the data currently stored
        in memcache.

        It returns the number of items that were successfully deleted.
        """
        if not data:
            raise CollectionNotFoundError
        if data["modified"] >= modified:
            raise ConflictError
        num_deleted = 0
        for id in items:
            if data["items"].pop(id, None) is not None:
                num_deleted += 1
        if num_deleted > 0:
            data["modified"] = modified
        key = self.get_key(userid)
        if not self.cache.cas(key, data, casid):
            raise ConflictError
        return num_deleted

    #
    # Methods whose implementation can be shared between subclasses.
    #

    def get_timestamp(self, userid):
        data, _ = self.get_cached_data(userid)
        if data is None:
            raise CollectionNotFoundError
        return data["modified"]

    def get_items(self, userid, items=None, **kwds):
        # Decode kwds into individual filter values.
        older = kwds.pop("older", None)
        newer = kwds.pop("newer", None)
        index_above = kwds.pop("index_above", None)
        index_below = kwds.pop("index_below", None)
        limit = kwds.pop("limit", None)
        sort = kwds.pop("sort", None)
        for unknown_kwd in kwds:
            raise TypeError("Unknown keyword argument: %s" % (unknown_kwd,))
        # Read all the items out of the cache.
        data, _ = self.get_cached_data(userid)
        if data is None:
            raise CollectionNotFoundError
        # Restrict to certain item ids if specified.
        if items is not None:
            bsos = (data["items"][item] for item in items)
        else:
            bsos = data["items"].itervalues()
        # Apply the various filters as generator expressions.
        if older is not None:
            bsos = (bso for bso in bsos if bso["modified"] < older)
        if newer is not None:
            bsos = (bso for bso in bsos if bso["modified"] > newer)
        if index_above is not None:
            bsos = (bso for bso in bsos if bso["sortindex"] > index_above)
        if index_below is not None:
            bsos = (bso for bso in bsos if bso["sortindex"] < index_below)
        # Filter out any that have expired.
        now = int(from_timestamp(get_timestamp()))
        later = now + 1
        bsos = (bso for bso in bsos if bso.get("ttl", later) > now)
        # Sort the resulting list if required.
        bsos = list(bsos)
        if sort is not None:
            if sort == "oldest":
                key = lambda bso: bso["modified"]
                reverse = False
            elif sort == "newer":
                key = lambda bso: bso["modified"]
                reverse = True
            else:
                key = lambda bso: bso["sortindex"]
                reverse = False
            bsos.sort(key=key, reverse=reverse)
        # Trim to the specified limit, if any.
        if limit is not None:
            bsos = bsos[:limit]
        return bsos

    def get_item_ids(self, userid, items=None, **kwds):
        items = self.get_items(userid, items, **kwds)
        return [bso["id"] for bso in items]

    def get_item(self, userid, item):
        items = self.get_items(userid, [item])
        if not items:
            raise ItemNotFoundError
        return items[0]

    def get_item_timestamp(self, userid, item):
        return self.get_item(userid, item)["modified"]


class CacheOnlyManager(_CachedManagerBase):
    """Object for managing storage of a collection solely in memcached.

    This manager class stores collection data in memcache without writing
    it through to the underlying store.  It generates its own timestamps
    internally and uses CAS to avoid conflicting writes.
    """

    def get_cached_data(self, userid):
        return self.cache.gets(self.get_key(userid))

    def set_items(self, userid, items):
        modified = get_timestamp()
        data, casid = self.get_cached_data(userid)
        self._set_items(userid, items, modified, data, casid)
        return modified

    def del_collection(self, userid):
        if not self.cache.delete(self.get_key(userid)):
            raise CollectionNotFoundError
        return get_timestamp()

    def del_items(self, userid, items):
        modified = get_timestamp()
        data, casid = self.get_cached_data(userid)
        self._del_items(userid, items, modified, data, casid)
        return data["modified"]

    def set_item(self, userid, item, bso):
        bso["id"] = item
        modified = get_timestamp()
        data, casid = self.get_cached_data(userid)
        num_created = self._set_items(userid, [bso], modified, data, casid)
        return {
            "created": num_created == 1,
            "modified": modified,
        }

    def del_item(self, userid, item):
        modified = get_timestamp()
        data, casid = self.get_cached_data(userid)
        num_deleted = self._del_items(userid, [item], modified, data, casid)
        if num_deleted == 0:
            raise ItemNotFoundError
        return modified


class CachedManager(_CachedManagerBase):
    """Object for managing storage of a collection in both cache and store.

    This manager class duplicates collection data from the underlying store
    into memcache, allowing faster access while guarding against data loss
    in the cache of memcache failure/purge.

    To avoid the cache getting out of sync with the underlying store, the
    cached data is deleted before any write operations and restored once
    they are known to have completed.  If something goes wrong, the cache
    data can be restored on next read from the known-good data in the
    underlying store.
    """

    def get_cached_data(self, userid, add_if_missing=True):
        """Get the cached collection data, pulling into cache if missing.

        This method returns the cached collection data, populating it from
        the underlying store if it is not cached.
        """
        key = self.get_key(userid)
        data, casid = self.cache.gets(key)
        if data is None:
            data = {}
            try:
                storage = self.storage
                collection = self.collection
                with self.owner.lock_for_read(userid, collection):
                    ts = storage.get_collection_timestamp(userid, collection)
                    data["modified"] = ts
                    data["items"] = {}
                    for bso in storage.get_items(userid, collection):
                        data["items"][bso["id"]] = bso
                if add_if_missing:
                    self.cache.add(key, data)
                    data, casid = self.cache.gets(key)
            except CollectionNotFoundError:
                data = None
        return data, casid

    def set_items(self, userid, items):
        storage = self.storage
        with self._mark_dirty(userid) as (data, casid):
            modified = storage.set_items(userid, self.collection, items)
        self._set_items(userid, items, modified, data, casid)
        return modified

    def del_collection(self, userid):
        self.cache.delete(self.get_key(userid))
        return self.storage.delete_collection(userid, self.collection)

    def del_items(self, userid, items):
        storage = self.storage
        with self._mark_dirty(userid) as (data, casid):
            modified = storage.delete_items(userid, self.collection, items)
        self._del_items(userid, items, modified, data, casid)
        return modified

    def set_item(self, userid, item, bso):
        storage = self.storage
        with self._mark_dirty(userid) as (data, casid):
            res = storage.set_item(userid, self.collection, item, bso)
        bso["id"] = item
        self._set_items(userid, [bso], res["modified"], data, casid)
        return res

    def del_item(self, userid, item):
        storage = self.storage
        with self._mark_dirty(userid) as (data, casid):
            modified = storage.delete_item(userid, self.collection, item)
        self._del_items(userid, [item], modified, data, casid)
        return modified

    @contextlib.contextmanager
    def _mark_dirty(self, userid):
        """Context manager to temporarily remove the cached data during write.

        All operations that may modify the underlying collection should be
        performed within this context manager.  It removes the data from cache
        before attempting the write, and rolls back to the cached version if
        it is safe to do so.

        Once the write operation has successfully completed
        """
        # Grad the current cache state so we can pass it to calling function.
        key = self.get_key(userid)
        data, casid = self.get_cached_data(userid, add_if_missing=False)
        # Remove it from the cache so that we don't serve stale data.
        # A CAS-DELETE here would be nice, but does memcached have one?
        if data is not None:
            self.cache.delete(key)
        # Yield control back the the calling function.
        # Since we've deleted the data, it should always use casid=None.
        try:
            yield data, None
        except StorageError:
            # If they get a storage-related error, it's safe to rollback
            # the cache. For any other sort of error we leave the cache clear.
            self.cache.add(key, data)
            raise

    def _set_items(self, userid, *args):
        """Update cached data with new items, or clear it on conflict.

        This method extends the base class _set_items method so that any
        failures are not bubbled up to the calling code.  By the time this
        method is called the write has already succeeded in the underlying
        store, so instead of reporting an error because of the cache, we
        just clear the cached data and let it re-populate on demand.
        """
        try:
            return super(CachedManager, self)._set_items(userid, *args)
        except StorageError:
            self.cache.delete(self.get_key(userid))

    def _del_items(self, userid, *args):
        """Update cached data with deleted items, or clear it on conflict.

        This method extends the base class _set_items method so that any
        failures are not bubbled up to the calling code.  By the time this
        method is called the write has already succeeded in the underlying
        store, so instead of reporting an error because of the cache, we
        just clear the cached data and let it re-populate on demand.
        """
        try:
            return super(CachedManager, self)._del_items(userid, *args)
        except StorageError:
            self.cache.delete(self.get_key(userid))
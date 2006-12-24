# Copyright (C) 2006 Jelmer Vernooij <jelmer@samba.org>

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

from bzrlib.errors import RevisionNotPresent, NotBranchError
from bzrlib.progress import ProgressBar
from bzrlib.revision import NULL_REVISION
from bzrlib.trace import mutter
from bzrlib.transport import get_transport
from bzrlib.knit import KnitVersionedFile
from warnings import warn

import os

import logwalker
from repository import (escape_svn_path, generate_svn_revision_id, 
                        parse_svn_revision_id, MAPPING_VERSION)

def generate_svn_file_id(uuid, revnum, branch, path):
    """Create a file id identifying a Subversion file.

    :param uuid: UUID of the repository
    :param revnu: Revision number at which the file was introduced.
    :param branch: Branch path of the branch in which the file was introduced.
    :param path: Original path of the file.
    """
    # FIXME: is the branch path required here?
    return "svn-v%d:%d@%s-%s-%s" % (MAPPING_VERSION, revnum, 
            uuid, escape_svn_path(branch), escape_svn_path(path))


def generate_file_id(revid, path):
    (uuid, branch, revnum) = parse_svn_revision_id(revid)
    return generate_svn_file_id(uuid, revnum, branch, path)


def get_local_changes(paths, scheme, uuid):
    new_paths = {}
    names = paths.keys()
    names.sort()
    for p in names:
        data = paths[p]
        new_p = scheme.unprefix(p)[1]
        if data[1] is not None:
            (cbp, crp) = scheme.unprefix(data[1])

            # Branch copy
            if (crp == "" and new_p == ""):
                data = ('M', None, None)
            else:
                data = (data[0], crp, generate_svn_revision_id(
                    uuid, data[2], cbp))

        new_paths[new_p] = data
    return new_paths

dbs = {}


class FileIdMap(object):
    """ File id store. 

    Keeps a map

    revnum -> branch -> path -> fileid
    """
    def __init__(self, log, cache_db):
        self._log = log
        self.cachedb = cache_db
        self.cachedb.executescript("""
        create table if not exists filemap (filename text, id integer, create_revid text, revid text);
        create index if not exists revid on filemap(revid);
        """)
        self.cachedb.commit()

    def save(self, revid, parent_revids, _map):
        mutter('saving file id map for %r' % revid)
        for filename in _map:
            self.cachedb.execute("insert into filemap (filename, id, create_revid, revid) values(?,?,?,?)", (filename, _map[filename][0], _map[filename][1], revid))
        self.cachedb.commit()

    def load(self, revid):
        map = {}
        for filename,create_revid,id in self.cachedb.execute("select filename, create_revid, id from filemap where revid='%s'"%revid):
            map[filename] = (id,create_revid)

        return map

    def apply_changes(self, uuid, revnum, branch, global_changes, map, 
            renames):
        """Change file id map to incorporate specified changes.

        :param uuid: UUID of repository changes happen in
        :param revnum: Revno for revision in which changes happened
        :param branch: Branch path where changes happened
        :param global_changes: Dict with global changes that happened
        """
        changes = get_local_changes(global_changes, self._log.scheme,
                                        uuid)

        def find_children(path, revid):
            (_, bp, revnum) = parse_svn_revision_id(revid)
            for p in self._log.find_children(bp+"/"+path, revnum):
                yield self._log.scheme.unprefix(p)[1]

        revid = generate_svn_revision_id(uuid, revnum, branch)

        return self._apply_changes(map, revid, changes, find_children, renames)

    def get_map(self, uuid, revnum, branch, pb=None, renames_cb=None):
        """Make sure the map is up to date until revnum."""
        if renames_cb is None:
            renames_cb = lambda x: {}
        # First, find the last cached map
        todo = []
        next_parent_revs = []
        
        if revnum == 0:
            return {}

        # No history -> empty map
        for (bp, paths, rev) in self._log.follow_history(branch, revnum):
            revid = generate_svn_revision_id(uuid, rev, bp)
            map = self.load(revid)
            if map != {}:
                # found the nearest cached map
                next_parent_revs = [revid]
                break
            else:
                todo.append((revid, paths))
                continue

        if len(next_parent_revs) == 0:
            if self._log.scheme.is_branch(""):
                map = {"": (generate_svn_file_id(uuid, 0, "", ""), NULL_REVISION)}
            else:
                map = {}
    
        # target revision was present
        if len(todo) == 0:
            return map
    
        todo.reverse()

        i = 0
        for (revid, global_changes) in todo:
            changes = get_local_changes(global_changes, self._log.scheme,
                                        uuid)
            mutter('generating file id map for %r' % revid)
            if pb is not None:
                pb.update('generating file id map', i, len(todo))

            def find_children(path, revid):
                (_, bp, revnum) = parse_svn_revision_id(revid)
                for p in self._log.find_children(bp+"/"+path, revnum):
                    yield self._log.scheme.unprefix(p)[1]

            parent_revs = next_parent_revs
            map = self._apply_changes(map, revid, changes, find_children, renames_cb(revid))
            next_parent_revs = [revid]
            i = i + 1

        if pb is not None:
            pb.clear()

        self.save(revid, parent_revs, map)
        return map


class SimpleFileIdMap(FileIdMap):
    @staticmethod
    def _apply_changes(map, revid, changes, find_children, renames):
        def new_file_id(path):
            mutter('new file id for %r. renames: %r' % (path, renames))
            if renames.has_key(path):
                return renames[path]
            return generate_file_id(revid, path)
        sorted_paths = changes.keys()
        sorted_paths.sort()
        for p in sorted_paths:
            data = changes[p]
            if data[0] in ('D', 'R'):
                assert map.has_key(p), "No map entry %s to delete/replace" % p
                del map[p]
                # Delete all children of p as well
                for c in map.keys():
                    if c.startswith(p+"/"):
                        del map[c]

            if data[0] in ('A', 'R'):
                map[p] = new_file_id(p), revid

                if not data[1] is None:
                    mutter('%r:%s copied from %r:%s' % (p, revid, data[1], data[2]))
                    if find_children is None:
                        warn('incomplete data for %r' % p)
                    else:
                        for c in find_children(data[1], data[2]):
                            map[c.replace(data[1], p, 1)] = new_file_id(c), revid

            elif data[0] == 'M':
                assert map.has_key(p), "Map has no item %s to modify" % p
                map[p] = map[p][0], revid
            
            # Mark all parent paths as changed
            parts = p.split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[0:len(parts)-i])
                assert map.has_key(parent), "Parent item %s of %s doesn't exist in map" % (parent, p)
                if map[parent][1] == revid:
                    break
                map[parent] = map[parent][0], revid
        return map

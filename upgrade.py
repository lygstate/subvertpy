# Copyright (C) 2006 by Jelmer Vernooij
# 
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""Upgrading revisions made with older versions of the mapping."""

from bzrlib import ui
from bzrlib.errors import BzrError, InvalidRevisionId
from bzrlib.revision import Revision
from bzrlib.trace import info

import itertools
from bzrlib.plugins.svn import changes, logwalker, mapping, properties
from bzrlib.plugins.svn.mapping import mapping_registry

class UpgradeChangesContent(BzrError):
    """Inconsistency was found upgrading the mapping of a revision."""
    _fmt = """Upgrade will change contents in revision %(revid)s. Use --allow-changes to override."""

    def __init__(self, revid):
        self.revid = revid



def create_upgraded_revid(revid, mapping_suffix, upgrade_suffix="-upgrade"):
    """Create a new revision id for an upgraded version of a revision.
    
    Prevents suffix to be appended needlessly.

    :param revid: Original revision id.
    :return: New revision id
    """
    if revid.endswith(upgrade_suffix):
        return revid[0:revid.rfind("-svn")] + mapping_suffix + upgrade_suffix
    else:
        return revid + mapping_suffix + upgrade_suffix


def determine_fileid_renames(old_tree, new_tree):
    for old_file_id in old_tree:
        new_file_id = new_tree.path2id(old_tree.id2path(old_file_id))
        if new_file_id is not None:
            yield old_file_id, new_file_id


def upgrade_workingtree(wt, svn_repository, new_mapping=None, 
                        allow_changes=False, verbose=False):
    """Upgrade a working tree.

    :param svn_repository: Subversion repository object
    """
    orig_basis_tree = wt.basis_tree()
    renames = upgrade_branch(wt.branch, svn_repository, new_mapping=new_mapping,
                             allow_changes=allow_changes, verbose=verbose)
    last_revid = wt.branch.last_revision()
    wt.set_last_revision(last_revid)

    # Adjust file ids in working tree
    for (old_fileid, new_fileid) in determine_fileid_renames(orig_basis_tree, wt.basis_tree()):
        path = wt.id2path(old_fileid)
        wt.remove(path)
        wt.add([path], [new_fileid])

    return renames


def upgrade_branch(branch, svn_repository, new_mapping=None, 
                   allow_changes=False, verbose=False):
    """Upgrade a branch to the current mapping version.
    
    :param branch: Branch to upgrade.
    :param svn_repository: Repository to fetch new revisions from
    :param allow_changes: Allow changes in mappings.
    :param verbose: Whether to print verbose list of rewrites
    """
    revid = branch.last_revision()
    renames = upgrade_repository(branch.repository, svn_repository, 
              revision_id=revid, new_mapping=new_mapping,
              allow_changes=allow_changes, verbose=verbose)
    if len(renames) > 0:
        branch.generate_revision_history(renames[revid])
    return renames


def check_revision_changed(oldrev, newrev):
    """Check if two revisions are different. This is exactly the same 
    as Revision.equals() except that it does not check the revision_id."""
    if (newrev.inventory_sha1 != oldrev.inventory_sha1 or
        newrev.timestamp != oldrev.timestamp or
        newrev.message != oldrev.message or
        newrev.timezone != oldrev.timezone or
        newrev.committer != oldrev.committer or
        newrev.properties != oldrev.properties):
        raise UpgradeChangesContent(oldrev.revision_id)


def generate_upgrade_map(new_mapping, revs):
    """Generate an upgrade map for use by bzr-rebase.

    :param new_mapping: BzrSvnMapping to upgrade revisions to.
    :param revs: Iterator over revisions to upgrade.
    :return: Map from old revids as keys, new revids as values stored in a 
             dictionary.
    """
    rename_map = {}
    # Create a list of revisions that can be renamed during the upgade
    for revid in revs:
        assert isinstance(revid, str)
        try:
            (uuid, bp, rev, mapping) = mapping_registry.parse_revision_id(revid)
        except InvalidRevisionId:
            # Not a bzr-svn revision, nothing to do
            continue
        newrevid = new_mapping.revision_id_foreign_to_bzr((uuid, rev, bp))
        if revid == newrevid:
            continue
        rename_map[revid] = newrevid

    return rename_map

MIN_REBASE_VERSION = (0, 4)

def create_upgrade_plan(repository, svn_repository, new_mapping,
                        revision_id=None, allow_changes=False):
    """Generate a rebase plan for upgrading revisions.

    :param repository: Repository to do upgrade in
    :param svn_repository: Subversion repository to fetch new revisions from.
    :param new_mapping: New mapping to use.
    :param revision_id: Revision to upgrade (None for all revisions in 
        repository.)
    :param allow_changes: Whether an upgrade is allowed to change the contents
        of revisions.
    :return: Tuple with a rebase plan and map of renamed revisions.
    """
    from bzrlib.plugins.svn import check_rebase_version
    from bzrlib.plugins.rebase.rebase import generate_transpose_plan
    check_rebase_version(MIN_REBASE_VERSION)

    graph = repository.get_graph()
    if revision_id is None:
        potential = repository.all_revision_ids()
    else:
        potential = itertools.imap(lambda (rev, parents): rev, 
                graph.iter_ancestry([revision_id]))
    upgrade_map = generate_upgrade_map(new_mapping, potential)
   
    # Make sure all the required current version revisions are present
    for revid in upgrade_map.values():
        if not repository.has_revision(revid):
            repository.fetch(svn_repository, revid)

    if not allow_changes:
        for oldrevid, newrevid in upgrade_map.items():
            oldrev = repository.get_revision(oldrevid)
            newrev = repository.get_revision(newrevid)
            check_revision_changed(oldrev, newrev)

    if revision_id is None:
        heads = repository.all_revision_ids() 
    else:
        heads = [revision_id]

    plan = generate_transpose_plan(graph.iter_ancestry(heads), upgrade_map, 
      graph,
      lambda revid: create_upgraded_revid(revid, new_mapping.upgrade_suffix))
    def remove_parents((oldrevid, (newrevid, parents))):
        return (oldrevid, newrevid)
    upgrade_map.update(dict(map(remove_parents, plan.items())))

    return (plan, upgrade_map)

 
def upgrade_repository(repository, svn_repository, new_mapping=None,
                       revision_id=None, allow_changes=False, verbose=False):
    """Upgrade the revisions in repository until the specified stop revision.

    :param repository: Repository in which to upgrade.
    :param svn_repository: Repository to fetch new revisions from.
    :param new_mapping: New mapping.
    :param revision_id: Revision id up until which to upgrade, or None for 
                        all revisions.
    :param allow_changes: Allow changes to mappings.
    :param verbose: Whether to print list of rewrites
    :return: Dictionary of mapped revisions
    """
    from bzrlib.plugins.svn import check_rebase_version
    check_rebase_version(MIN_REBASE_VERSION)
    from bzrlib.plugins.rebase.rebase import (
        replay_snapshot, rebase, rebase_todo)

    if new_mapping is None:
        new_mapping = svn_repository.get_mapping()

    # Find revisions that need to be upgraded, create
    # dictionary with revision ids in key, new parents in value
    try:
        repository.lock_write()
        svn_repository.lock_read()
        (plan, revid_renames) = create_upgrade_plan(repository, svn_repository, 
                                                    new_mapping,
                                                    revision_id=revision_id,
                                                    allow_changes=allow_changes)
        if verbose:
            for revid in rebase_todo(repository, plan):
                info("%s -> %s" % (revid, plan[revid][0]))
        def fix_revid(revid):
            try:
                (uuid, bp, rev, mapping) = mapping_registry.parse_revision_id(revid)
            except InvalidRevisionId:
                return revid
            return new_mapping.revision_id_foreign_to_bzr((uuid, rev, bp))
        def replay(repository, oldrevid, newrevid, new_parents):
            return replay_snapshot(repository, oldrevid, newrevid, new_parents,
                                   revid_renames, fix_revid)
        rebase(repository, plan, replay)
        return revid_renames
    finally:
        repository.unlock()
        svn_repository.unlock()


def set_revprops(repository, new_mapping, from_revnum=0, to_revnum=None):
    """Set bzr-svn revision properties for existing bzr-svn revisions.

    :param repository: Subversion Repository object.
    :param new_mapping: Mapping to upgrade to
    """
    if to_revnum is None:
        to_revnum = repository.get_latest_revnum()
    graph = repository.get_graph()
    assert from_revnum <= to_revnum
    pb = ui.ui_factory.nested_progress_bar()
    logcache = getattr(repository._log, "cache", None)
    try:
        for (paths, revnum, revprops) in repository._log.iter_changes(None, to_revnum, from_revnum, pb=pb):
            if revnum == 0:
                # Never a bzr-svn revision
                continue
            # Find the root path of the change
            bp = changes.changes_root(paths.keys())
            if bp is None:
                fileprops = {}
            else:
                fileprops = logwalker.lazy_dict({}, repository.branchprop_list.get_properties, bp, revnum)
            old_mapping = mapping.find_mapping(revprops, fileprops)
            if old_mapping is None:
                # Not a bzr-svn revision
                if not mapping.SVN_REVPROP_BZR_SKIP in revprops:
                    repository.transport.change_rev_prop(revnum, mapping.SVN_REVPROP_BZR_SKIP, "")
                continue
            if old_mapping == new_mapping:
                # Already the latest mapping
                continue
            assert old_mapping.supports_custom_revprops() or bp is not None
            new_revprops = dict(revprops.items())
            revmeta = repository._revmeta(bp, changes, revnum, revprops, fileprops)
            rev = revmeta.get_revision(old_mapping)
            revno = graph.find_distance_to_null(rev.revision_id, [])
            assert bp is not None
            new_mapping.export_revision(bp, rev.timestamp, rev.timezone, rev.committer, rev.properties, rev.revision_id, revno, rev.parent_ids, new_revprops, None)
            new_mapping.export_fileid_map(old_mapping.import_fileid_map(revprops, fileprops), 
                new_revprops, None)
            new_mapping.export_text_parents(old_mapping.import_text_parents(revprops, fileprops),
                new_revprops, None)
            if rev.message != mapping.parse_svn_log(revprops.get(properties.PROP_REVISION_LOG)):
                new_mapping.export_message(rev.message, new_revprops, None)
            changed_revprops = dict(filter(lambda (k,v): k not in revprops or revprops[k] != v, new_revprops.items()))
            if logcache is not None:
                logcache.drop_revprops(revnum)
            for k, v in changed_revprops.items():
                repository.transport.change_rev_prop(revnum, k, v)
            # Might as well update the cache while we're at it
    finally:
        pb.finished()

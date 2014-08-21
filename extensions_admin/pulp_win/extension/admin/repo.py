# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

from gettext import gettext as _
import logging
from urlparse import urlparse

from pulp.client.arg_utils import (InvalidConfig, convert_file_contents,
                                   convert_removed_options)
from pulp.client.commands.repo.cudl import (CreateRepositoryCommand, ListRepositoriesCommand,
                                            UpdateRepositoryCommand)
from pulp.client.commands import options as std_options
from pulp.common import constants as pulp_constants
from pulp.common.util import encode_unicode

from pulp_win.common import constants, ids
from pulp_win.common.ids import WIN_DISTRIBUTOR_ID
from pulp_win.extension.admin import repo_options

# -- constants ----------------------------------------------------------------

DESC_SEARCH = _('searches for WIN repositories on the server')

# Tuples of importer key name to more user-friendly CLI name. This must be a
# list of _all_ importer config values as the process of building up the
# importer config starts by extracting all of these values from the user args.
IMPORTER_CONFIG_KEYS = []

WIN_DISTRIBUTOR_CONFIG_KEYS = [
    ('relative_url', 'relative_url'),
    ('http', 'serve_http'),
    ('https', 'serve_https'),
    ('checksum_type', 'checksum_type'),
    ('skip', 'skip'),
]

LOG = logging.getLogger(__name__)


class MsiRepoCreateCommand(CreateRepositoryCommand):

    def __init__(self, context):
        super(MsiRepoCreateCommand, self).__init__(context)

        # The built-in options will be reorganized under a group to keep the
        # help text from being unwieldly. The base class will add them by
        # default, so remove them here before they are readded under a group.
        self.options = []

        repo_options.add_to_command(self)

    def run(self, **kwargs):

        # Gather data
        repo_id = kwargs.pop(std_options.OPTION_REPO_ID.keyword)
        description = kwargs.pop(std_options.OPTION_DESCRIPTION.keyword, None)
        display_name = kwargs.pop(std_options.OPTION_NAME.keyword, None)
        notes = kwargs.pop(std_options.OPTION_NOTES.keyword) or {}

        # Add a note to indicate this is an WIN repository
        notes[pulp_constants.REPO_NOTE_TYPE_KEY] = constants.REPO_NOTE_WIN

        try:
            importer_config = args_to_importer_config(kwargs)
            win_distributor_config = args_to_win_distributor_config(kwargs)
        except InvalidConfig, e:
            self.prompt.render_failure_message(str(e))
            return

        # During create (but not update), if the relative path isn't specified
        # it is derived from the feed_url
        if 'relative_url' not in win_distributor_config:
            win_distributor_config['relative_url'] = repo_id

        # Both http and https must be specified in the distributor config, so
        # make sure they are initially set here (default to only https)
        if 'http' not in win_distributor_config and 'https' not in win_distributor_config:
            win_distributor_config['https'] = True
            win_distributor_config['http'] = False

        # Make sure both are referenced
        for k in ('http', 'https'):
            if k not in win_distributor_config:
                win_distributor_config[k] = False

        # Package distributors for the call
        distributors = [
            dict(distributor_type=ids.TYPE_ID_DISTRIBUTOR_WIN, distributor_config=win_distributor_config,
                 auto_publish=True, distributor_id=ids.WIN_DISTRIBUTOR_ID),
        ]

        # Create the repository; let exceptions bubble up to the framework exception handler
        self.context.server.repo.create_and_configure(
            repo_id, display_name, description, notes,
            ids.TYPE_ID_IMPORTER_WIN, importer_config, distributors
        )

        msg = _('Successfully created repository [%(r)s]')
        self.prompt.render_success_message(msg % {'r' : repo_id})


class MsiRepoUpdateCommand(UpdateRepositoryCommand):

    def __init__(self, context):
        super(MsiRepoUpdateCommand, self).__init__(context)

        # The built-in options will be reorganized under a group to keep the
        # help text from being unwieldly. The base class will add them by
        # default, so remove them here before they are readded under a group.
        self.options = []

        repo_options.add_to_command(self)

    def run(self, **kwargs):

        # Gather data
        repo_id = kwargs.pop(std_options.OPTION_REPO_ID.keyword)
        description = kwargs.pop(std_options.OPTION_DESCRIPTION.keyword, None)
        display_name = kwargs.pop(std_options.OPTION_NAME.keyword, None)
        notes = kwargs.pop(std_options.OPTION_NOTES.keyword, None)

        try:
            importer_config = args_to_importer_config(kwargs)
        except InvalidConfig, e:
            self.prompt.render_failure_message(str(e))
            return

        try:
            win_distributor_config = args_to_win_distributor_config(kwargs)
        except InvalidConfig, e:
            self.prompt.render_failure_message(str(e))
            return

        distributor_configs = {ids.TYPE_ID_DISTRIBUTOR_WIN : win_distributor_config }

        response = self.context.server.repo.update_repo_and_plugins(
            repo_id, display_name, description, notes,
            importer_config, distributor_configs
        )

        if not response.is_async():
            msg = _('Repository [%(r)s] successfully updated')
            self.prompt.render_success_message(msg % {'r' : repo_id})
        else:
            msg = _('Repository update postponed due to another operation. '
                    'Progress on this task can be viewed using the commands '
                    'under "repo tasks"')
            self.prompt.render_paragraph(msg)
            self.prompt.render_reasons(response.response_body.reasons)


class MsiRepoListCommand(ListRepositoriesCommand):

    def __init__(self, context):
        repos_title = _('MSI Repositories')
        super(MsiRepoListCommand, self).__init__(context, repos_title=repos_title)

        # Both get_repositories and get_other_repositories will act on the full
        # list of repositories. Lazy cache the data here since both will be
        # called in succession, saving the round trip to the server.
        self.all_repos_cache = None

    def get_repositories(self, query_params, **kwargs):
        all_repos = self._all_repos(query_params, **kwargs)

        msi_repos = []
        for repo in all_repos:
            notes = repo['notes']
            if pulp_constants.REPO_NOTE_TYPE_KEY in notes and notes[pulp_constants.REPO_NOTE_TYPE_KEY] == constants.REPO_NOTE_WIN:
                msi_repos.append(repo)

        # There isn't really anything compelling in the exporter distributor
        # to display to the user, so remove it entirely.
        for r in msi_repos:
            if 'distributors' in r:
                r['distributors'] = [x for x in r['distributors'] if x['id'] == WIN_DISTRIBUTOR_ID]

        # Strip out the certificate and private key if present
        for r in msi_repos:
            # The importers will only be present in a --details view, so make
            # sure it's there before proceeding
            if 'importers' not in r:
                continue

            imp_config = r['importers'][0]['config'] # there can only be one importer

        return msi_repos

    def get_other_repositories(self, query_params, **kwargs):
        all_repos = self._all_repos(query_params, **kwargs)

        non_msi_repos = []
        for repo in all_repos:
            notes = repo['notes']
            if notes.get(pulp_constants.REPO_NOTE_TYPE_KEY, None) != constants.REPO_NOTE_WIN:
                non_msi_repos.append(repo)

        return non_msi_repos

    def _all_repos(self, query_params, **kwargs):

        # This is safe from any issues with concurrency due to how the CLI works
        if self.all_repos_cache is None:
            self.all_repos_cache = self.context.server.repo.repositories(query_params).response_body

        return self.all_repos_cache


# -- utilities ----------------------------------------------------------------

def args_to_importer_config(kwargs):
    """
    Takes the arguments read from the CLI and converts the client-side input
    to the server-side expectations. The supplied dict will not be modified.

    @return: config to pass into the add/update importer calls
    @raise InvalidConfig: if one or more arguments is not valid for the importer
    """

    importer_config = _prep_config(kwargs, IMPORTER_CONFIG_KEYS)

    LOG.debug('Importer configuration options')
    LOG.debug(importer_config)
    return importer_config


def args_to_win_distributor_config(kwargs):
    """
    Takes the arguments read from the CLI and converts the client-side input
    to the server-side expectations. The supplied dict will not be modified.

    @return: config to pass into the add/update distributor calls
    @raise InvalidConfig: if one or more arguments is not valid for the distributor
    """
    distributor_config = _prep_config(kwargs, WIN_DISTRIBUTOR_CONFIG_KEYS)

    LOG.debug('Distributor configuration options')
    LOG.debug(distributor_config)

    return distributor_config


def _prep_config(kwargs, plugin_config_keys):
    """
    Performs common initialization for both importer and distributor config
    parsing. The common conversion includes:

    * Create a base config dict pulling the given plugin_config_keys from the
      user-specified arguments
    * Translate the client-side argument names into the plugin expected keys
    * Strip out any None values which means the user did not specify the
      argument in the call
    * Convert any empty strings into None which represents the user removing
      the config value

    @param plugin_config_keys: one of the *_CONFIG_KEYS constants
    @return: dictionary to use as the basis for the config
    """

    # User-specified flags use hyphens but the importer/distributor want
    # underscores, so do a quick translation here before anything else.
    for k in kwargs.keys():
        v = kwargs.pop(k)
        new_key = k.replace('-', '_')
        kwargs[new_key] = v

    # Populate the plugin config with the plugin-relevant keys in the user args
    user_arg_keys = [k[1] for k in plugin_config_keys]
    plugin_config = dict([(k, v) for k, v in kwargs.items() if k in user_arg_keys])

    # Simple name translations
    for plugin_key, cli_key in plugin_config_keys:
        plugin_config[plugin_key] = plugin_config.pop(cli_key, None)

    # Apply option removal conventions
    convert_removed_options(plugin_config)

    return plugin_config
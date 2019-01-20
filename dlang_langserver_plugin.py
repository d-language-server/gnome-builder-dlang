import gi
import os

from gi.repository import GLib
from gi.repository import Gio
from gi.repository import GObject
from gi.repository import Ide


class DlangService(Ide.Object, Ide.Service):
    _client = None
    _has_started = False
    _monitor = None

    @GObject.Property(type=Ide.LangservClient)
    def client(self):
        return self._client

    @client.setter
    def client(self, value):
        self._client = value
        self.notify("client")

    def do_context_loaded(self):
        context = self.get_context()
        project_file = context.get_project_file()
        project_dir = project_file if project_file.query_file_type(0, None) == Gio.FileType.DIRECTORY \
            else project_file.get_parent()
        selections_file = project_dir.get_child("dub.selections.json")
        self._monitor = selections_file.monitor(0, None)
        self._monitor.set_rate_limit(1000)
        self._monitor.connect("changed", self._monitor_changed_cb)

    def _monitor_changed_cb(self, monitor, file, other_file, event_type):
        if self._client and file.get_basename() == "dub.selections.json":
            file_event = GLib.Variant("a{sv}", { "uri": GLib.Variant.new_string(file.get_uri()), "type": GLib.Variant.new_byte(2) })
            files_params = GLib.Variant("a{sav}", { "changes": [file_event] })
            self._client.send_notification_async("workspace/didChangeWatchedFiles", files_params, None, self._dls_notification_finish, None)

    def do_stop(self):
        if self._monitor is not None:
            monitor, self._monitor = self._monitor, None
            monitor.cancel()

        if self._client is not None:
            client, self._client = self._client, None
            client.stop()

    def _ensure_started(self):
        if self._has_started:
            return

        self._has_started = True
        launcher = self._create_launcher()
        launcher.set_clear_env(False)

        workdir = self.get_context().get_vcs().get_working_directory()
        launcher.set_cwd(workdir.get_path())
        launcher.push_argv(os.path.join(GLib.get_home_dir(), ".dub", "packages", ".bin", "dls-latest", "dls"))
        launcher.push_argv("--stdio")

        supervisor = Ide.SubprocessSupervisor()
        supervisor.connect("spawned", self._dls_spawned)
        supervisor.set_launcher(launcher)
        supervisor.start()

    def _dls_spawned(self, supervisor, subprocess):
        stdin = subprocess.get_stdin_pipe()
        stdout = subprocess.get_stdout_pipe()
        io_stream = Gio.SimpleIOStream.new(stdout, stdin)

        if self._client:
            self._client.stop()

        settings = GLib.Variant("a{sv}", { "symbol": GLib.Variant("a{sb}", { "listLocalSymbols": True }) })
        dls_settings = GLib.Variant("a{sv}", { "d": GLib.Variant("a{sv}", { "dls": settings }) })
        config_params = GLib.Variant("a{sv}", { "settings": dls_settings })

        self._client = Ide.LangservClient.new(self.get_context(), io_stream)
        self._client.add_language("d")
        self._client.start()
        self._client.send_notification_async("initialized", None, None, self._dls_notification_finish, None)
        self._client.send_notification_async("workspace/didChangeConfiguration", config_params, None, self._dls_notification_finish, None)
        self.notify("client")

    def _dls_notification_finish(self, source_object, result, user_data):
        if self._client:
            self._client.send_notification_finish(result)

    def _create_launcher(self):
        flags = Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
        launcher = Ide.SubprocessLauncher()
        launcher.set_flags(flags)
        launcher.set_cwd(GLib.get_home_dir())
        launcher.set_run_on_host(True)
        return launcher

    @classmethod
    def bind_client(klass, provider):
        context = provider.get_context()
        self = context.get_service_typed(DlangService)
        self._ensure_started()
        self.bind_property("client", provider, "client", GObject.BindingFlags.SYNC_CREATE)

try:
    class DlangDiagnosticProvider(Ide.LangservDiagnosticProvider):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangCompletionProvider(Ide.LangservCompletionProvider):
        def do_load(self, context):
            DlangService.bind_client(self)

        def do_get_priority(self, context):
            return -1000
except AttributeError: pass

try:
    class DlangRenameProvider(Ide.LangservRenameProvider):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangSymbolResolver(Ide.LangservSymbolResolver):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangHighlighter(Ide.LangservHighlighter):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangFormatter(Ide.LangservFormatter):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangHoverProvider(Ide.LangservHoverProvider):
        def do_prepare(self):
            self.props.category = "D"

        def do_load(self, context):
            DlangService.bind_client(self)
except AttributeError: pass

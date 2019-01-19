import gi
import os

from gi.repository import GLib
from gi.repository import Gio
from gi.repository import GObject
from gi.repository import Ide


class DlangService(Ide.Object, Ide.Service):
    _client = None
    _has_started = False

    @GObject.Property(type=Ide.LangservClient)
    def client(self):
        return self._client

    @client.setter
    def client(self, value):
        self._client = value
        self.notify("client")

    def do_stop(self):
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

        self._client = Ide.LangservClient.new(self.get_context(), io_stream)
        self._client.add_language("d")
        self._client.start()
        self._client.send_notification_async("initialized", None, None, self._dls_notification_finish, None)
        self.notify("client")

    def _dls_notification_finish(self, source_object, result, user_data):
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

class DlangDiagnosticProvider(Ide.LangservDiagnosticProvider):
    def do_load(self):
        DlangService.bind_client(self)

class DlangCompletionProvider(Ide.LangservCompletionProvider):
    def do_load(self, context):
        DlangService.bind_client(self)

    def do_get_priority(self, context):
        return -1000

class DlangRenameProvider(Ide.LangservRenameProvider):
    def do_load(self):
        DlangService.bind_client(self)

class DlangSymbolResolver(Ide.LangservSymbolResolver):
    def do_load(self):
        DlangService.bind_client(self)

class DlangHighlighter(Ide.LangservHighlighter):
    def do_load(self):
        DlangService.bind_client(self)

class DlangFormatter(Ide.LangservFormatter):
    def do_load(self):
        DlangService.bind_client(self)

class DlangHoverProvider(Ide.LangservHoverProvider):
    def do_prepare(self):
        self.props.category = "D"

    def do_load(self, context):
        DlangService.bind_client(self)

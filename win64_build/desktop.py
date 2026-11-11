"""Self-contained Qt desktop window and observable backend startup."""
import ctypes
import json
import logging
import os
from pathlib import Path
import sys
import time
import traceback

READY_PREFIX = 'DICT_SERVER_READY '
STARTUP_TIMEOUT = 120
PAGE_TIMEOUT = 45


def portable_root():
    return Path(os.environ.get('DICT_PORTABLE_ROOT', Path(sys.argv[0]).resolve().parent)).resolve()


def configure_browser():
    # Avoid another application's Qt installation and problematic GPU drivers.
    for name in ('QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QML2_IMPORT_PATH'):
        os.environ.pop(name, None)
    flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = flags + ' --disable-gpu --disable-gpu-compositing'
    os.environ['QT_QUICK_BACKEND'] = 'software'


def parse_page_state(value):
    # Qt reliably returns JSON strings; plain JS objects are not portable across
    # bindings/backends and can be delivered as None despite successful JS.
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def run_window(root, data, smoke_test=False):
    from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkRequest
    from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView

    log = logging.getLogger('desktop')
    qInstallMessageHandler(lambda kind, context, message: log.info('Qt: %s', message))
    QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
    application = QApplication.instance() or QApplication(['LanguageTrainer'])

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):
            log.info('JavaScript %s %s:%s %s', level, source, line, message)

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.started = time.monotonic()
            self.page_started = None
            self.html_loaded = False
            self.url = None
            self.ready = False
            self.failed = False
            self.closing = False
            self.output = ''
            self.stage = 'Starting local application'
            self.setWindowTitle('LanguageTrainer — starting')
            self.resize(1280, 850)
            self.setMinimumSize(900, 600)
            self.stack = QStackedWidget()
            self.setCentralWidget(self.stack)
            status = QWidget()
            layout = QVBoxLayout(status)
            title = QLabel('LanguageTrainer')
            title.setStyleSheet('font-size:24px;font-weight:600;padding:12px')
            self.status = QLabel(self.stage)
            self.status.setWordWrap(True)
            self.details = QPlainTextEdit()
            self.details.setReadOnly(True)
            self.details.setMaximumBlockCount(150)
            retry = QPushButton('Retry')
            retry.clicked.connect(lambda: self.restart())
            logs = QPushButton('Open log folder')
            logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data))))
            for widget in (title, self.status, self.details, retry, logs):
                layout.addWidget(widget)
            self.stack.addWidget(status)
            self.view = QWebEngineView()
            profile = QWebEngineProfile.defaultProfile()
            profile.setPersistentStoragePath(str(data / 'browser'))
            profile.setCachePath(str(data / 'browser/cache'))
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
            self.page = Page(profile, self.view)
            self.view.setPage(self.page)
            self.stack.addWidget(self.view)
            self.view.loadFinished.connect(self.page_loaded)
            self.page.renderProcessTerminated.connect(lambda status, code: self.fail(f'Browser renderer stopped: {status}, exit code {code}'))
            self.network = QNetworkAccessManager(self)
            self.process = QProcess(self)
            self.process.readyReadStandardOutput.connect(self.read_output)
            self.process.readyReadStandardError.connect(self.read_error)
            self.process.errorOccurred.connect(lambda error: self.fail(f'Backend process error: {self.process.errorString()}'))
            self.process.finished.connect(lambda code, status: self.fail(f'Backend stopped, exit code {code}') if not self.closing else None)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(500)
            QTimer.singleShot(0, self.start_backend)

        def note(self, message):
            log.info(message)
            self.details.appendPlainText(message)

        def fail(self, message):
            if self.closing or self.failed:
                return
            self.failed = True
            self.ready = False
            self.stack.setCurrentIndex(0)
            self.setWindowTitle('LanguageTrainer — startup error')
            self.status.setText(message + '\nDetails are saved in ' + str(data / 'desktop.log'))
            self.note('ERROR: ' + message)
            if smoke_test:
                application.exit(1)

        def start_backend(self):
            environment = QProcessEnvironment.systemEnvironment()
            for name in ('PYTHONHOME', 'PYTHONPATH'):
                environment.remove(name)
            for name, value in {
                'DICT_ROOT_DIR': str(root / 'resources'), 'DICT_DATA_DIR': str(data),
                'DICT_ARGOS_ROOT': str(data / 'argos-translate'),
                'NLTK_DATA': str(root / 'resources/nltk_data'), 'PYTHONNOUSERSITE': '1',
            }.items():
                environment.insert(name, value)
            self.process.setProcessEnvironment(environment)
            self.process.setWorkingDirectory(str(root))
            self.note('Starting bundled backend: ' + str(root / 'backend/python.exe'))
            self.process.start(str(root / 'backend/python.exe'), ['-u', '-X', 'utf8', str(root / 'backend/dict.py'), 'server', '--host', '127.0.0.1', '--port', '0', '--init'])

        def read_error(self):
            text = bytes(self.process.readAllStandardError()).decode('utf-8', 'replace')
            if text.strip():
                self.note(text.strip())

        def read_output(self):
            self.output += bytes(self.process.readAllStandardOutput()).decode('utf-8', 'replace')
            while '\n' in self.output:
                line, self.output = self.output.split('\n', 1)
                self.note(line)
                if line.startswith(READY_PREFIX):
                    try:
                        url = json.loads(line[len(READY_PREFIX):])['url']
                        parsed = QUrl(url)
                        if parsed.scheme() != 'http' or parsed.host() != '127.0.0.1' or parsed.port() <= 0:
                            raise ValueError('Invalid local backend URL')
                        self.url = url
                        self.stage = 'Checking local backend'
                        reply = self.network.get(QNetworkRequest(QUrl(url.rstrip('/') + '/api/health')))
                        reply.finished.connect(lambda: self.health_checked(reply))
                    except Exception as error:
                        self.fail('Invalid backend readiness response: ' + str(error))

        def health_checked(self, reply):
            try:
                payload = json.loads(bytes(reply.readAll()).decode('utf-8'))
                if payload.get('status') != 'ok':
                    raise ValueError('Health check did not return OK')
                self.stage = 'Loading interface'
                self.html_loaded = False
                self.page_started = time.monotonic()
                self.view.setUrl(QUrl(self.url))
            except Exception as error:
                self.fail('Local backend health check failed: ' + str(error))
            finally:
                reply.deleteLater()

        def page_loaded(self, success):
            if self.closing or self.failed or self.page_started is None:
                return
            if not success:
                self.fail('The interface page could not be loaded.')
            else:
                self.html_loaded = True
                self.note('Interface HTML loaded; waiting for application readiness')

        def checked_page(self, state):
            if self.failed or self.closing or self.ready:
                return
            state = parse_page_state(state)
            if state.get('error'):
                self.fail('Interface startup error: ' + str(state['error']))
            elif state.get('ready'):
                self.ready = True
                self.stack.setCurrentWidget(self.view)
                self.setWindowTitle('LanguageTrainer')
                self.note('Application ready')
                if smoke_test:
                    application.exit(0)

        def tick(self):
            if self.failed or self.closing or self.ready:
                return
            elapsed = time.monotonic() - self.started
            self.status.setText(f'{self.stage} — {elapsed:.0f}s\nLog: {data / "desktop.log"}')
            if self.page_started is not None:
                if self.html_loaded:
                    self.page.runJavaScript('JSON.stringify({ready: !!window.__LANGUAGE_TRAINER_READY__, error: window.__LANGUAGE_TRAINER_BOOT_ERROR__ || ""})', self.checked_page)
                if time.monotonic() - self.page_started > PAGE_TIMEOUT:
                    self.fail('The interface did not initialize within 45 seconds. See the JavaScript errors in the log.')
            elif elapsed > STARTUP_TIMEOUT:
                self.fail('The backend did not become ready within 120 seconds. The last startup messages are shown below.')

        def stop_backend(self):
            self.closing = True
            if self.process.state() != QProcess.ProcessState.NotRunning:
                self.process.terminate()
                if not self.process.waitForFinished(3000):
                    self.process.kill()
                    self.process.waitForFinished(1000)

        def restart(self):
            self.stop_backend()
            self.closing = self.failed = self.ready = False
            self.started = time.monotonic()
            self.page_started = self.url = None
            self.html_loaded = False
            self.stage = 'Restarting local application'
            self.output = ''
            self.start_backend()

        def closeEvent(self, event):
            self.stop_backend()
            event.accept()

    window = Window()
    window.show()
    result = application.exec()
    window.stop_backend()
    return result


def main():
    log = None
    try:
        root = portable_root()
        data = Path(os.environ.get('DICT_DESKTOP_DATA_DIR', root / 'data'))
        data.mkdir(parents=True, exist_ok=True)
        log = (data / 'desktop.log').open('a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = log
        logging.basicConfig(level=logging.INFO, stream=log, format='%(asctime)s %(levelname)s %(message)s', force=True)
        logging.info('Desktop launch: root=%s Python=%s', root, sys.executable)
        configure_browser()
        return run_window(root, data, smoke_test='--smoke-test' in sys.argv)
    except Exception as error:
        if log:
            traceback.print_exc(file=log)
        message = f'{error}\n\nIf present, see data/desktop.log next to LanguageTrainer.exe.'
        if sys.platform == 'win32' and '--smoke-test' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, message, 'LanguageTrainer startup error', 0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

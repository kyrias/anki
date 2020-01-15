# Copyright: Ankitects Pty Ltd and contributors
# -*- coding: utf-8 -*-
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import json
import math
import sys

from anki.lang import _
from anki.utils import isLin, isMac, isWin
from aqt import gui_hooks
from aqt.qt import *
from aqt.utils import openLink

# Page for debug messages
##########################################################################


class AnkiWebPage(QWebEnginePage):  # type: ignore
    def __init__(self, onBridgeCmd):
        QWebEnginePage.__init__(self)
        self._onBridgeCmd = onBridgeCmd
        self._setupBridge()

    def _setupBridge(self):
        class Bridge(QObject):
            @pyqtSlot(str, result=str)
            def cmd(self, str):
                return json.dumps(self.onCmd(str))

        self._bridge = Bridge()
        self._bridge.onCmd = self._onCmd

        self._channel = QWebChannel(self)
        self._channel.registerObject("py", self._bridge)
        self.setWebChannel(self._channel)

        js = QFile(":/qtwebchannel/qwebchannel.js")
        assert js.open(QIODevice.ReadOnly)
        js = bytes(js.readAll()).decode("utf-8")

        script = QWebEngineScript()
        script.setSourceCode(
            js
            + """
            var pycmd;
            new QWebChannel(qt.webChannelTransport, function(channel) {
                pycmd = function (arg, cb) {
                    var resultCB = function (res) {
                        // pass result back to user-provided callback
                        if (cb) {
                            cb(JSON.parse(res));
                        }
                    }
                
                    channel.objects.py.cmd(arg, resultCB);
                    return false;                   
                }
                pycmd("domDone");
            });
        """
        )
        script.setWorldId(QWebEngineScript.MainWorld)
        script.setInjectionPoint(QWebEngineScript.DocumentReady)
        script.setRunsOnSubFrames(False)
        self.profile().scripts().insert(script)

    def javaScriptConsoleMessage(self, lvl, msg, line, srcID):
        # not translated because console usually not visible,
        # and may only accept ascii text
        buf = "JS error on line %(a)d: %(b)s" % dict(a=line, b=msg + "\n")
        # ensure we don't try to write characters the terminal can't handle
        buf = buf.encode(sys.stdout.encoding, "backslashreplace").decode(
            sys.stdout.encoding
        )
        sys.stdout.write(buf)

    def acceptNavigationRequest(self, url, navType, isMainFrame):
        if not isMainFrame:
            return True
        # data: links generated by setHtml()
        if url.scheme() == "data":
            return True
        # catch buggy <a href='#' onclick='func()'> links
        from aqt import mw

        if url.matches(QUrl(mw.serverURL()), QUrl.RemoveFragment):
            print("onclick handler needs to return false")
            return False
        # load all other links in browser
        openLink(url)
        return False

    def _onCmd(self, str):
        return self._onBridgeCmd(str)


# Main web view
##########################################################################


class AnkiWebView(QWebEngineView):  # type: ignore
    def __init__(self, parent=None):
        QWebEngineView.__init__(self, parent=parent)
        self.title = "default"
        self._page = AnkiWebPage(self._onBridgeCmd)
        self._page.setBackgroundColor(self._getWindowColor())  # reduce flicker

        self._domDone = True
        self._pendingActions = []
        self.requiresCol = True
        self.setPage(self._page)

        self._page.profile().setHttpCacheType(QWebEngineProfile.NoCache)
        self.resetHandlers()
        self.allowDrops = False
        self._filterSet = False
        QShortcut(
            QKeySequence("Esc"),
            self,
            context=Qt.WidgetWithChildrenShortcut,
            activated=self.onEsc,
        )
        if isMac:
            for key, fn in [
                (QKeySequence.Copy, self.onCopy),
                (QKeySequence.Paste, self.onPaste),
                (QKeySequence.Cut, self.onCut),
                (QKeySequence.SelectAll, self.onSelectAll),
            ]:
                QShortcut(
                    key, self, context=Qt.WidgetWithChildrenShortcut, activated=fn
                )
            QShortcut(
                QKeySequence("ctrl+shift+v"),
                self,
                context=Qt.WidgetWithChildrenShortcut,
                activated=self.onPaste,
            )

    def eventFilter(self, obj, evt):
        # disable pinch to zoom gesture
        if isinstance(evt, QNativeGestureEvent):
            return True
        elif evt.type() == QEvent.MouseButtonRelease:
            if evt.button() == Qt.MidButton and isLin:
                self.onMiddleClickPaste()
                return True
            return False
        return False

    def onEsc(self):
        w = self.parent()
        while w:
            if isinstance(w, QDialog) or isinstance(w, QMainWindow):
                from aqt import mw

                # esc in a child window closes the window
                if w != mw:
                    w.close()
                else:
                    # in the main window, removes focus from type in area
                    self.parent().setFocus()
                break
            w = w.parent()

    def onCopy(self):
        self.triggerPageAction(QWebEnginePage.Copy)

    def onCut(self):
        self.triggerPageAction(QWebEnginePage.Cut)

    def onPaste(self):
        self.triggerPageAction(QWebEnginePage.Paste)

    def onMiddleClickPaste(self):
        self.triggerPageAction(QWebEnginePage.Paste)

    def onSelectAll(self):
        self.triggerPageAction(QWebEnginePage.SelectAll)

    def contextMenuEvent(self, evt):
        m = QMenu(self)
        a = m.addAction(_("Copy"))
        a.triggered.connect(self.onCopy)
        gui_hooks.webview_will_show_context_menu(self, m)
        m.popup(QCursor.pos())

    def dropEvent(self, evt):
        pass

    def setHtml(self, html):
        # discard any previous pending actions
        self._pendingActions = []
        self._domDone = True
        self._queueAction("setHtml", html)

    def _setHtml(self, html):
        app = QApplication.instance()
        oldFocus = app.focusWidget()
        self._domDone = False
        self._page.setHtml(html)
        # work around webengine stealing focus on setHtml()
        if oldFocus:
            oldFocus.setFocus()

    def zoomFactor(self):
        # overridden scale factor?
        webscale = os.environ.get("ANKI_WEBSCALE")
        if webscale:
            return float(webscale)

        if isMac:
            return 1
        screen = QApplication.desktop().screen()
        dpi = screen.logicalDpiX()
        factor = dpi / 96.0
        if isLin:
            factor = max(1, factor)
            return factor
        # compensate for qt's integer scaling on windows?
        if qtminor >= 14:
            return 1
        qtIntScale = self._getQtIntScale(screen)
        desiredScale = factor * qtIntScale
        newFactor = desiredScale / qtIntScale
        return max(1, newFactor)

    def _getQtIntScale(self, screen):
        # try to detect if Qt has scaled the screen
        # - qt will round the scale factor to a whole number, so a dpi of 125% = 1x,
        #   and a dpi of 150% = 2x
        # - a screen with a normal physical dpi of 72 will have a dpi of 32
        #   if the scale factor has been rounded to 2x
        # - different screens have different physical DPIs (eg 72, 93, 102)
        # - until a better solution presents itself, assume a physical DPI at
        #   or above 70 is unscaled
        if screen.physicalDpiX() > 70:
            return 1
        elif screen.physicalDpiX() > 35:
            return 2
        else:
            return 3

    def _getWindowColor(self):
        if isMac:
            # standard palette does not return correct window color on macOS
            return QColor("#ececec")
        return self.style().standardPalette().color(QPalette.Window)

    def stdHtml(self, body, css=None, js=None, head=""):
        if css is None:
            css = []
        if js is None:
            js = ["jquery.js"]

        palette = self.style().standardPalette()
        color_hl = palette.color(QPalette.Highlight).name()

        if isWin:
            # T: include a font for your language on Windows, eg: "Segoe UI", "MS Mincho"
            family = _('"Segoe UI"')
            widgetspec = "button { font-size: 12px; font-family:%s; }" % family
            widgetspec += "\n:focus { outline: 1px solid %s; }" % color_hl
            fontspec = "font-size:12px;font-family:%s;" % family
        elif isMac:
            family = "Helvetica"
            fontspec = 'font-size:15px;font-family:"%s";' % family
            widgetspec = """
button { font-size: 13px; -webkit-appearance: none; background: #fff; border: 1px solid #ccc;
border-radius:5px; font-family: Helvetica }"""
        else:
            family = self.font().family()
            color_hl_txt = palette.color(QPalette.HighlightedText).name()
            color_btn = palette.color(QPalette.Button).name()
            fontspec = 'font-size:14px;font-family:"%s";' % family
            widgetspec = """
/* Buttons */
button{ font-size:14px; -webkit-appearance:none; outline:0;
        background-color: %(color_btn)s; border:1px solid rgba(0,0,0,.2);
        border-radius:2px; height:24px; font-family:"%(family)s"; }
button:focus{ border-color: %(color_hl)s }
button:hover{ background-color:#fff }
button:active, button:active:hover { background-color: %(color_hl)s; color: %(color_hl_txt)s;}
/* Input field focus outline */
textarea:focus, input:focus, input[type]:focus, .uneditable-input:focus,
div[contenteditable="true"]:focus {   
    outline: 0 none;
    border-color: %(color_hl)s;
}""" % {
                "family": family,
                "color_btn": color_btn,
                "color_hl": color_hl,
                "color_hl_txt": color_hl_txt,
            }

        csstxt = "\n".join(
            [self.bundledCSS("webview.css")] + [self.bundledCSS(fname) for fname in css]
        )
        jstxt = "\n".join(
            [self.bundledScript("webview.js")]
            + [self.bundledScript(fname) for fname in js]
        )
        from aqt import mw

        head = mw.baseHTML() + head + csstxt + jstxt

        html = """
<!doctype html>
<html><head>
<title>{}</title>

<style>
body {{ zoom: {}; background: {}; {} }}
{}
</style>
  
{}
</head>

<body>{}</body>
</html>""".format(
            self.title,
            self.zoomFactor(),
            self._getWindowColor().name(),
            fontspec,
            widgetspec,
            head,
            body,
        )
        # print(html)
        self.setHtml(html)

    def webBundlePath(self, path):
        from aqt import mw

        return "http://127.0.0.1:%d/_anki/%s" % (mw.mediaServer.getPort(), path)

    def bundledScript(self, fname):
        return '<script src="%s"></script>' % self.webBundlePath(fname)

    def bundledCSS(self, fname):
        return '<link rel="stylesheet" type="text/css" href="%s">' % self.webBundlePath(
            fname
        )

    def eval(self, js):
        self.evalWithCallback(js, None)

    def evalWithCallback(self, js, cb):
        self._queueAction("eval", js, cb)

    def _evalWithCallback(self, js, cb):
        if cb:

            def handler(val):
                if self._shouldIgnoreWebEvent():
                    print("ignored late js callback", cb)
                    return
                cb(val)

            self.page().runJavaScript(js, handler)
        else:
            self.page().runJavaScript(js)

    def _queueAction(self, name, *args):
        self._pendingActions.append((name, args))
        self._maybeRunActions()

    def _maybeRunActions(self):
        while self._pendingActions and self._domDone:
            name, args = self._pendingActions.pop(0)

            if name == "eval":
                self._evalWithCallback(*args)
            elif name == "setHtml":
                self._setHtml(*args)
            else:
                raise Exception("unknown action: {}".format(name))

    def _openLinksExternally(self, url):
        openLink(url)

    def _shouldIgnoreWebEvent(self):
        # async web events may be received after the profile has been closed
        # or the underlying webview has been deleted
        from aqt import mw

        if sip.isdeleted(self):
            return True
        if not mw.col and self.requiresCol:
            return True
        return False

    def _onBridgeCmd(self, cmd):
        if self._shouldIgnoreWebEvent():
            print("ignored late bridge cmd", cmd)
            return

        if not self._filterSet:
            self.focusProxy().installEventFilter(self)
            self._filterSet = True

        if cmd == "domDone":
            self._domDone = True
            self._maybeRunActions()
        else:
            return self.onBridgeCmd(cmd)

    def defaultOnBridgeCmd(self, cmd):
        print("unhandled bridge cmd:", cmd)

    def resetHandlers(self):
        self.onBridgeCmd = self.defaultOnBridgeCmd

    def adjustHeightToFit(self):
        self.evalWithCallback("$(document.body).height()", self._onHeight)

    def _onHeight(self, qvar):
        from aqt import mw

        if qvar is None:

            mw.progress.timer(1000, mw.reset, False)
            return

        scaleFactor = self.zoomFactor()
        if scaleFactor == 1:
            scaleFactor = mw.pm.uiScale()

        height = math.ceil(qvar * scaleFactor)
        self.setFixedHeight(height)

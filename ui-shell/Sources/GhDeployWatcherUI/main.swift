import AppKit
import WebKit

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

if CommandLine.arguments.contains("--selftest") {
    let failures = runSelfTest()
    if failures.isEmpty { print("selftest ok"); exit(0) }
    for f in failures { FileHandle.standardError.write(Data(("FAIL: " + f + "\n").utf8)) }
    exit(1)
}

// Nothing is created or loaded until the first stdin line has been validated.
guard let line = readLine(strippingNewline: true) else {
    fail("gh-deploy-watcher-ui: no URL on stdin", code: 2)
}
guard let startURL = parseStartURL(line), let appPort = startURL.port else {
    fail("gh-deploy-watcher-ui: refusing URL (expected http://127.0.0.1:<port>/...)", code: 2)
}

final class WindowController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let port: Int
    let window: NSWindow
    let webView: WKWebView
    private var confirmed = false

    init(url: URL, port: Int) {
        self.port = port
        let config = WKWebViewConfiguration()
        config.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        // No WKScriptMessageHandler is registered: the page has no bridge to native code.
        webView = WKWebView(frame: .zero, configuration: config)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 720),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window.title = "gh-deploy-watcher"
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.contentView = webView
        window.delegate = self
        window.center()
        webView.load(URLRequest(url: url))  // fragment (token) is kept
    }

    private func openExternally(_ url: URL?) {
        guard let url = url, let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else { return }
        NSWorkspace.shared.open(url)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, isAppURL(url, port: port) {
            decisionHandler(.allow)
        } else {
            openExternally(navigationAction.request.url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        openExternally(navigationAction.request.url)
        return nil
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if confirmed { return true }
        webView.evaluateJavaScript("window.ghdwDirty === true") { [weak self] result, _ in
            guard let self = self else { return }
            if (result as? Bool) == true {
                let alert = NSAlert()
                alert.messageText = "Discard unsaved changes?"
                alert.informativeText = "Your selection has changes that were not saved."
                alert.addButton(withTitle: "Cancel")
                alert.addButton(withTitle: "Discard")
                if alert.runModal() != .alertSecondButtonReturn { return }
            }
            self.confirmed = true
            self.window.close()
        }
        return false
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var controller: WindowController?
    let url: URL
    let port: Int

    init(url: URL, port: Int) {
        self.url = url
        self.port = port
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let c = WindowController(url: url, port: port)
        controller = c
        c.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate(url: startURL, port: appPort)
app.delegate = delegate
app.run()

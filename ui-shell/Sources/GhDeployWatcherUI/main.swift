import AppKit
import WebKit

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

if CommandLine.arguments.contains("--selftest") {
    let failures = runSelfTest() + runMenuSelfTest()
    if failures.isEmpty { print("selftest ok; menu checks ok"); exit(0) }
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
    let startURL: URL
    let window: NSWindow
    let webView: WKWebView
    private(set) var confirmed = false

    init(url: URL, port: Int) {
        self.port = port
        self.startURL = url
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
            if navigationAction.navigationType == .linkActivated {
                openExternally(navigationAction.request.url)
            }
            decisionHandler(.cancel)
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.load(URLRequest(url: startURL))
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if navigationAction.navigationType == .linkActivated {
            openExternally(navigationAction.request.url)
        }
        return nil
    }

    /// Single close flow shared by window close and app quit. Callers arriving
    /// while a check is running are queued, so there is never a second alert.
    /// `completion(true)` means it is fine to go ahead.
    private var checking = false
    private var waiting: [(Bool) -> Void] = []

    func confirmClose(_ completion: @escaping (Bool) -> Void) {
        if confirmed { completion(true); return }
        waiting.append(completion)
        if checking { return }
        checking = true
        var answered = false
        func finish(_ ok: Bool) {
            checking = false
            let callbacks = waiting
            waiting = []
            if ok { confirmed = true }
            callbacks.forEach { $0(ok) }
        }
        // If the page does not answer, close without asking rather than trap the user.
        let timeout = DispatchWorkItem { [weak self] in
            if self == nil || answered { return }
            answered = true
            finish(true)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0, execute: timeout)
        webView.evaluateJavaScript("window.ghdwDirty === true") { [weak self] result, _ in
            guard let self = self, !answered else { return }
            answered = true
            timeout.cancel()
            guard (result as? Bool) == true else { finish(true); return }
            let alert = NSAlert()
            alert.messageText = "Discard unsaved changes?"
            alert.informativeText = "Your selection has changes that were not saved."
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Discard")
            alert.beginSheetModal(for: self.window) { response in
                finish(response == .alertSecondButtonReturn)
            }
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if confirmed { return true }
        confirmClose { [weak self] ok in
            if ok { self?.window.close() }
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
        NSApp.mainMenu = buildMainMenu()
        c.window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let c = controller, !c.confirmed else { return .terminateNow }
        c.confirmClose { ok in NSApp.reply(toApplicationShouldTerminate: ok) }
        return .terminateLater
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate(url: startURL, port: appPort)
app.delegate = delegate
app.run()

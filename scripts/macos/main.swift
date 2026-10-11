// Job Search: the Dock app for the local web app (macOS).
//
// One window showing the app's UI. It reuses a backend already answering on the port (one
// started by scripts/dev.sh or a terminal), otherwise runs scripts/start.sh and stops that
// backend on quit. macOS keeps one copy of an app running, so clicking the Dock icon brings
// this window back (un-minimised) instead of opening another. Built by make_app.sh, which
// writes the repository path, the PATH of the shell that built it and the port into Info.plist.

import AppKit
import WebKit

struct Config {
    let root: String
    let path: String
    let port: Int

    var home: URL { URL(string: "http://localhost:\(port)/")! }
    var health: URL { URL(string: "http://127.0.0.1:\(port)/api/health")! }
    var devUI: URL { URL(string: "http://localhost:5173/")! }
    var log: URL { URL(fileURLWithPath: root).appendingPathComponent(".run/logs/app.log") }

    static func load() -> Config {
        let info = Bundle.main.infoDictionary ?? [:]
        return Config(
            root: info["JobSearchRoot"] as? String ?? "",
            path: info["JobSearchPath"] as? String ?? "/usr/bin:/bin:/usr/sbin:/sbin",
            port: info["JobSearchPort"] as? Int ?? 8000
        )
    }
}

enum Backend { case ready, devOnly, down }

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
    WKDownloadDelegate
{
    let config = Config.load()
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var starting = false
    var downloads: [ObjectIdentifier: URL] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = makeMenu()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()  // keeps the UI's remembered state
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        if #available(macOS 13.3, *) { webView.isInspectable = true }

        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        window = NSWindow(
            contentRect: screen.insetBy(dx: screen.width * 0.05, dy: screen.height * 0.05),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Job Search"
        window.isReleasedWhenClosed = false  // closing hides it; the Dock icon brings it back
        window.contentView = webView
        window.setFrameAutosaveName("JobSearchMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        start()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow()
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) {
        if let server, server.isRunning { server.terminate() }  // only the one this app started
    }

    func showWindow() {
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: backend

    func start() {
        guard !starting else { return }
        starting = true
        Task {
            await connect()
            starting = false
        }
    }

    func connect() async {
        status("Starting Job Search…", detail: "The first start installs and builds the app, which can take a few minutes.")
        var launched = false
        var grace = 6  // start.sh may exit because another backend took the port: let it come up
        for _ in 0..<1200 {  // up to 10 minutes: a first start installs and builds
            switch await probe() {
            case .ready: webView.load(URLRequest(url: config.home)); return
            case .devOnly where await reachable(config.devUI):
                webView.load(URLRequest(url: config.devUI)); return
            default: break
            }
            if server?.isRunning != true {
                if !launched {
                    launched = true
                    if !launchServer() { return }
                } else {
                    grace -= 1
                    if grace == 0 { break }
                }
            }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        status("Job Search could not start", detail: "See \(config.log.path) for details.", retry: true)
    }

    func probe() async -> Backend {
        var request = URLRequest(url: config.health)
        request.timeoutInterval = 1
        guard let (data, response) = try? await URLSession.shared.data(for: request),
            (response as? HTTPURLResponse)?.statusCode == 200,
            let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            body["app"] as? String == "job_search"
        else { return .down }
        return body["ui"] as? Bool == true ? .ready : .devOnly
    }

    func reachable(_ url: URL) async -> Bool {
        var request = URLRequest(url: url)
        request.timeoutInterval = 1
        return (try? await URLSession.shared.data(for: request)) != nil
    }

    func launchServer() -> Bool {
        let script = URL(fileURLWithPath: config.root).appendingPathComponent("scripts/start.sh")
        guard FileManager.default.fileExists(atPath: script.path) else {
            status("Repository not found", detail: "\(config.root) has no scripts/start.sh. Rebuild the app with scripts/macos/make_app.sh.")
            return false
        }
        try? FileManager.default.createDirectory(
            at: config.log.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: config.log.path, contents: nil)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script.path]
        process.currentDirectoryURL = URL(fileURLWithPath: config.root)
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = config.path  // apps opened from the Dock get a bare PATH
        env["BACKEND_PORT"] = String(config.port)
        process.environment = env
        if let log = try? FileHandle(forWritingTo: config.log) {
            process.standardOutput = log
            process.standardError = log
        }
        do { try process.run() } catch { return false }
        server = process
        return true
    }

    func status(_ title: String, detail: String, retry: Bool = false) {
        let action = retry ? "<p><a href=\"jobsearch://retry\">Try again</a></p>" : ""
        let html = """
            <html><head><meta charset="utf-8"><style>
            :root { color-scheme: light dark; --fg: #1f2328; --muted: #656d76; --bg: #ffffff; }
            @media (prefers-color-scheme: dark) { :root { --fg: #e6edf3; --muted: #8d96a0; --bg: #0d1117; } }
            body { margin: 0; height: 100vh; display: grid; place-items: center; background: var(--bg);
                   color: var(--fg); font: 14px -apple-system, system-ui, sans-serif; text-align: center; }
            p { color: var(--muted); max-width: 32rem; } a { color: inherit; }
            </style></head><body><div><h2>\(title)</h2><p>\(detail)</p>\(action)</div></body></html>
            """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: navigation: app pages here, other sites in the default browser, files to Downloads

    func webView(
        _ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
        decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = action.request.url else { return decisionHandler(.allow) }
        if url.scheme == "jobsearch" {
            start()
            return decisionHandler(.cancel)
        }
        if action.shouldPerformDownload { return decisionHandler(.download) }
        let local = url.host == "localhost" || url.host == "127.0.0.1"
        if !local && ["http", "https"].contains(url.scheme ?? "") && action.targetFrame?.isMainFrame != false {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
        decisionHandler: @escaping @MainActor (WKNavigationResponsePolicy) -> Void
    ) {
        let disposition = (response.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition") ?? ""
        let attachment = disposition.lowercased().hasPrefix("attachment")
        decisionHandler(attachment || !response.canShowMIMEType ? .download : .allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code == NSURLErrorCancelled { return }
        start()  // the backend went away (e.g. dev.sh restarted it): wait for it, or start one
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }

    func download(
        _ download: WKDownload, decideDestinationUsing response: URLResponse,
        suggestedFilename: String, completionHandler: @escaping @MainActor (URL?) -> Void
    ) {
        let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        let name = (suggestedFilename as NSString)
        var target = folder.appendingPathComponent(suggestedFilename)
        var copy = 1
        while FileManager.default.fileExists(atPath: target.path) {
            copy += 1
            let ext = name.pathExtension.isEmpty ? "" : ".\(name.pathExtension)"
            target = folder.appendingPathComponent("\(name.deletingPathExtension) (\(copy))\(ext)")
        }
        downloads[ObjectIdentifier(download)] = target
        completionHandler(target)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let target = downloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
        // Bounces the Downloads stack in the Dock, as browsers do.
        DistributedNotificationCenter.default().post(
            name: Notification.Name("com.apple.DownloadFileFinished"), object: target.path)
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloads.removeValue(forKey: ObjectIdentifier(download))
        alert("Download failed", error.localizedDescription)
    }

    // MARK: browser features the UI uses: new tabs, file pickers, confirm dialogs

    func webView(
        _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
        for action: WKNavigationAction, windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }  // e.g. Gmail sign-in
        return nil
    }

    func webView(
        _ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    func webView(
        _ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor () -> Void
    ) {
        alert(message, "")
        completionHandler()
    }

    func webView(
        _ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor (Bool) -> Void
    ) {
        let box = NSAlert()
        box.messageText = message
        box.addButton(withTitle: "OK")
        box.addButton(withTitle: "Cancel")
        completionHandler(box.runModal() == .alertFirstButtonReturn)
    }

    func webView(
        _ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?, initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @MainActor (String?) -> Void
    ) {
        let box = NSAlert()
        box.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = defaultText ?? ""
        box.accessoryView = field
        box.addButton(withTitle: "OK")
        box.addButton(withTitle: "Cancel")
        completionHandler(box.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
    }

    func alert(_ title: String, _ text: String) {
        let box = NSAlert()
        box.messageText = title
        box.informativeText = text
        box.runModal()
    }

    // MARK: menu (the Edit menu is what makes copy and paste work in the page)

    @objc func reloadPage() {
        if webView.url?.scheme == "about" { start() } else { webView.reload() }
    }
    @objc func zoomIn() { webView.pageZoom = min(webView.pageZoom + 0.1, 3) }
    @objc func zoomOut() { webView.pageZoom = max(webView.pageZoom - 0.1, 0.5) }
    @objc func zoomReset() { webView.pageZoom = 1 }

    func makeMenu() -> NSMenu {
        let bar = NSMenu()
        func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            let holder = NSMenuItem()
            holder.submenu = menu
            bar.addItem(holder)
            return menu
        }
        func item(
            _ title: String, _ action: Selector?, _ key: String,
            _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil
        ) -> NSMenuItem {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
            entry.keyEquivalentModifierMask = mods
            entry.target = target  // nil: the responder chain (window, web view) handles it
            return entry
        }
        _ = submenu("Job Search", [
            item("About Job Search", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            .separator(),
            item("Hide Job Search", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:)), ""),
            .separator(),
            item("Quit Job Search", #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = submenu("View", [
            item("Reload", #selector(reloadPage), "r", target: self),
            .separator(),
            item("Actual Size", #selector(zoomReset), "0", target: self),
            item("Zoom In", #selector(zoomIn), "=", target: self),
            item("Zoom Out", #selector(zoomOut), "-", target: self),
            .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        NSApp.windowsMenu = submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:)), ""),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        return bar
    }
}

let delegate = MainActor.assumeIsolated { AppDelegate() }
NSApplication.shared.delegate = delegate
NSApplication.shared.setActivationPolicy(.regular)
NSApplication.shared.run()

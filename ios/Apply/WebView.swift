import SwiftUI
import WebKit

/// Drive / autopilot lifecycle for the in-app browser.
enum DriveState: Equatable {
    case idle
    case probing
    case filling
    case advancing
    case needsHuman(String)
    case watchingClear
    case paused
    case ready
    case failed(String)

    var isRunning: Bool {
        switch self {
        case .filling, .advancing, .probing: return true
        default: return false
        }
    }
}

/// Shared WKWebView storage so Greenhouse / Workable / Okta logins survive
/// across applications and app launches. Never use a non-persistent store.
enum ApplyBrowserSession {
    static let dataStore = WKWebsiteDataStore.default()
}

/// Owns the WKWebView for one application. The profile is injected on every page
/// load (so it survives multi-step navigation and reaches iframes); the native
/// Autofill button starts Simplify-style autopilot (`__applyDrive`).
final class WebViewModel: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    let webView: WKWebView
    @Published var loading = false
    @Published var canGoBack = false
    /// The page the WebView is actually on (after redirects).
    @Published var currentURL: String? = nil
    @Published var lastFill: (filled: Int, essays: Int, rules: String)? = nil
    @Published var fillSeq: Int = 0
    /// Labels this Fill pass skipped (unmatched / empty / no listed option).
    @Published var lastSkips: [[String: Any]] = []
    @Published var driveState: DriveState = .idle
    @Published var driveStep: Int = 0
    @Published var statusLine: String = ""
    /// True while a one-shot Fill is in flight (propeller on the Fill button).
    @Published var oneShotFilling = false
    /// `login` / `captcha` / `application` / … from the last form probe.
    @Published var lastProbeKind: String? = nil

    private var backObserver: NSKeyValueObservation?
    /// Careers sites wrap the real form in a cross-origin iframe.
    /// `evaluateJavaScript` only runs in the main frame, so we hop once.
    private var didHopIntoEmbed = false
    /// True while `__applyDrive({mode:'watch'})` is expected to be running.
    private var startedWatch = false
    /// A Next/Apply click that loads a whole new document destroys the in-page
    /// autopilot along with the rest of the JS context. SmartRecruiters does this
    /// on "I'm interested", and so does any multi-page ATS — the loop simply died
    /// mid-run and left the banner reading "Advancing…" for ever. Pick it up on
    /// the new document instead, a bounded number of times so a page that
    /// navigates every time it is driven can't loop.
    private var driveResumes = 0
    private let maxDriveResumes = 3

    init(identity: [String: String], answers: [Question], rules: RulesPayload? = nil) {
        let controller = WKUserContentController()
        controller.addUserScript(WKUserScript(
            source: Autofill.dataScript(identity: identity, answers: answers, rules: rules),
            injectionTime: .atDocumentStart, forMainFrameOnly: false))
        controller.addUserScript(WKUserScript(
            source: Autofill.lib, injectionTime: .atDocumentEnd, forMainFrameOnly: false))

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = ApplyBrowserSession.dataStore
        cfg.userContentController = controller
        cfg.defaultWebpagePreferences.allowsContentJavaScript = true
        webView = WKWebView(frame: .zero, configuration: cfg)
        super.init()
        controller.add(self, name: "applyfill")
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        backObserver = webView.observe(\.canGoBack, options: [.new]) { [weak self] wv, _ in
            DispatchQueue.main.async { self?.canGoBack = wv.canGoBack }
        }
    }

    deinit {
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "applyfill")
    }

    // "Apply" buttons on job sites often open a new tab (target="_blank" / window.open).
    func webView(_ w: WKWebView, createWebViewWith config: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if action.targetFrame == nil, action.request.url != nil { w.load(action.request) }
        return nil
    }

    func load(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        didHopIntoEmbed = false
        startedWatch = false
        lastProbeKind = nil
        driveState = .idle
        statusLine = ""
        webView.load(URLRequest(url: url))
    }

    /// Start Simplify-style autopilot (fill → Next → refill). Never submits.
    func startAutopilot() {
        startedWatch = false
        driveResumes = 0
        driveState = .probing
        statusLine = "Looking for the form…"
        Theme.impact(.soft)
        webView.evaluateJavaScript("window.__applyDrive && window.__applyDrive({ mode: 'run' })")
    }

    func pauseAutopilot() {
        webView.evaluateJavaScript("window.__applyDrive && window.__applyDrive({ mode: 'pause' })")
        driveState = .paused
        statusLine = "Paused — edit freely, then resume"
        Theme.selection()
    }

    func resumeAutopilot() {
        startedWatch = false
        driveState = .probing
        statusLine = "Resuming…"
        Theme.impact(.soft)
        webView.evaluateJavaScript("window.__applyDrive && window.__applyDrive({ mode: 'run' })")
    }

    /// One-shot fill. Pauses instead of typing into a login wall or CAPTCHA.
    func autofill() {
        Theme.impact(.soft)
        oneShotFilling = true
        webView.evaluateJavaScript(
            "window.__applyFillOrPause ? window.__applyFillOrPause() : (window.__applyAutofillAll ? window.__applyAutofillAll() : (window.__applyAutofill && window.__applyAutofill()))")
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
            self?.oneShotFilling = false
        }
    }
    func goBack() { if webView.canGoBack { webView.goBack() } }

    func webView(_ w: WKWebView, didStartProvisionalNavigation n: WKNavigation!) {
        loading = true
        currentURL = w.url?.absoluteString
        canGoBack = w.canGoBack
        // A new document kills in-page JS watchers; allow a fresh watch after load.
        startedWatch = false
    }
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        loading = false
        currentURL = w.url?.absoluteString
        canGoBack = w.canGoBack
        hopIntoAtsEmbedIfNeeded(w) { [weak self] hopped in
            guard let self, !hopped else { return }
            // The autopilot was mid-run when this page replaced the one it lived
            // in. Carry on here rather than leaving it stuck on the last status.
            if self.driveState.isRunning, self.driveResumes < self.maxDriveResumes {
                self.driveResumes += 1
                self.statusLine = "Picking up on the next page…"
                w.evaluateJavaScript("window.__applyDrive && window.__applyDrive({ mode: 'run' })")
                return
            }
            // Soft probe so the banner updates after redirects (skip if we just
            // navigated into an embed — that load will probe on its own didFinish).
            w.evaluateJavaScript("window.__applyDrive && window.__applyDrive({ mode: 'probe' })")
    }

    /// If this careers page only hosts the application inside an ATS iframe, navigate
    /// into that embed so fill/probe run against real `<input>`s in the main frame.
    private func hopIntoAtsEmbedIfNeeded(_ w: WKWebView, completion: @escaping (Bool) -> Void) {
        guard !didHopIntoEmbed else { completion(false); return }
        w.evaluateJavaScript("window.__applyFindApplyEmbed && window.__applyFindApplyEmbed()") { [weak self] result, _ in
            guard let self,
                  let urlStr = result as? String,
                  let url = URL(string: urlStr) else {
                DispatchQueue.main.async { completion(false) }
                return
            }
            DispatchQueue.main.async {
                guard !self.didHopIntoEmbed else { completion(false); return }
                self.didHopIntoEmbed = true
                self.statusLine = "Opening the application form…"
                self.webView.load(URLRequest(url: url))
                completion(true)
            }
        }
    }
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) {
        loading = false
        currentURL = w.url?.absoluteString
        canGoBack = w.canGoBack
    }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        loading = false
        currentURL = w.url?.absoluteString
        canGoBack = w.canGoBack
    }

    func userContentController(_ u: WKUserContentController, didReceive m: WKScriptMessage) {
        guard m.name == "applyfill", let d = m.body as? [String: Any] else { return }
        DispatchQueue.main.async { self.handleDriveMessage(d) }
    }

    private func handleDriveMessage(_ d: [String: Any]) {
        let filled = d["filled"] as? Int ?? 0
        let essays = d["essays"] as? Int ?? 0
        let rules = d["rules"] as? String ?? "?"
        let step = d["step"] as? Int ?? driveStep
        driveStep = step

        let status = (d["status"] as? String) ?? "filled"
        let probe = d["probe"] as? [String: Any]
        if let kind = probe?["kind"] as? String {
            lastProbeKind = kind
        }
        // Only real fill reports update lastFill — probe/drive status pings also
        // carry filled:0 and would otherwise flash a false "no fields" toast.
        if status == "filled" {
            lastFill = (filled, essays, rules)
            lastSkips = d["skips"] as? [[String: Any]] ?? []
            fillSeq += 1
            oneShotFilling = false
        }

        switch status {
        case "probe":
            applyIdleProbe(probe)
            if driveState == .probing { statusLine = "Reading the page…" }
        case "filling":
            driveState = .filling
            statusLine = step > 0 ? "Filling step \(step + 1)…" : "Filling fields…"
        case "advancing":
            driveState = .advancing
            let detail = d["detail"] as? String
            statusLine = detail == "reveal" ? "Opening the application…" : "Advancing…"
            Theme.selection()
        case "needsHuman":
            showBlocker(probe: probe, fallback: d["blocker"] as? String)
            startWatchIfNeeded()
        case "watchingClear":
            startedWatch = false
            driveState = .watchingClear
            statusLine = "Clear — tap Resume to fill"
            Theme.notify(.success)
        case "paused":
            driveState = .paused
            statusLine = "Paused — you can fill manually"
        case "ready":
            driveState = .ready
            statusLine = "Cleared — review & submit yourself"
            Theme.notify(.success)
        case "failed":
            let reason = (d["blocker"] as? String) ?? "Couldn’t drive this page"
            driveState = .failed(reason)
            statusLine = reason
            Theme.notify(.error)
        case "filled":
            // One-shot fill report from __applyAutofill during a drive step.
            if driveState.isRunning || driveState == .probing {
                statusLine = filled > 0 ? "Filled \(filled)…" : statusLine
            }
        default:
            break
        }
    }

    /// Soft-probe after navigation: surface login/CAPTCHA immediately, and
    /// promote a cleared wall to Resume. Does not start filling.
    private func applyIdleProbe(_ probe: [String: Any]?) {
        let kind = (probe?["kind"] as? String) ?? "unknown"
        switch driveState {
        case .filling, .advancing, .probing, .paused, .ready:
            return
        default:
            break
        }
        if kind == "login" || kind == "captcha" {
            showBlocker(probe: probe, fallback: nil)
            startWatchIfNeeded()
            return
        }
        if case .needsHuman = driveState {
            startedWatch = false
            driveState = .watchingClear
            statusLine = "Clear — tap Resume to fill"
            Theme.notify(.success)
            return
        }
        if driveState == .idle {
            if kind == "application" {
                statusLine = "Form ready — tap Fill"
            } else if let reveal = probe?["revealLabel"] as? String, !reveal.isEmpty {
                statusLine = "Tap Fill to open the application"
            }
        }
    }

    private func showBlocker(probe: [String: Any]?, fallback: String?) {
        let kind = (probe?["kind"] as? String) ?? lastProbeKind ?? ""
        lastProbeKind = kind.isEmpty ? lastProbeKind : kind
        let entering: Bool
        if case .needsHuman = driveState { entering = false }
        else { entering = true }
        driveState = .needsHuman(Self.blockerTitle(kind: kind, fallback: fallback))
        statusLine = Self.blockerDetail(kind: kind)
        if entering { Theme.notify(.warning) }
        oneShotFilling = false
    }

    private func startWatchIfNeeded() {
        guard !startedWatch else { return }
        guard !driveState.isRunning else { return }
        startedWatch = true
        webView.evaluateJavaScript("window.__applyDrive && window.__applyDrive({ mode: 'watch' })")
    }

    static func blockerTitle(kind: String, fallback: String?) -> String {
        switch kind {
        case "login": return "Sign in on this page"
        case "captcha": return "This site is checking you're human"
        default: return fallback?.isEmpty == false ? fallback! : "This page needs you"
        }
    }

    static func blockerDetail(kind: String) -> String {
        switch kind {
        case "login":
            return "Sign-in stays in JobPilot for the next application."
        case "captcha":
            return "Complete the check here, then tap Resume."
        default:
            return "Handle this, then tap Resume."
        }
    }
}

/// Bridges the model's WKWebView into SwiftUI.
struct WebViewContainer: UIViewRepresentable {
    let model: WebViewModel
    func makeUIView(context: Context) -> WKWebView { model.webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

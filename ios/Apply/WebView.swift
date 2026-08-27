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
    @Published var driveState: DriveState = .idle
    @Published var driveStep: Int = 0
    @Published var statusLine: String = ""

    private var backObserver: NSKeyValueObservation?
    /// Careers sites (Instacart, etc.) wrap the real Greenhouse/Lever/Ashby form in a
    /// cross-origin iframe. `evaluateJavaScript` only runs in the main frame, so we
    /// hop into that embed once — otherwise Autofill sees zero fields and no-ops.
    private var didHopIntoEmbed = false

    init(identity: [String: String], answers: [Question], rules: RulesPayload? = nil) {
        let controller = WKUserContentController()
        controller.addUserScript(WKUserScript(
            source: Autofill.dataScript(identity: identity, answers: answers, rules: rules),
            injectionTime: .atDocumentStart, forMainFrameOnly: false))
        controller.addUserScript(WKUserScript(
            source: Autofill.lib, injectionTime: .atDocumentEnd, forMainFrameOnly: false))

        let cfg = WKWebViewConfiguration()
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

    // "Apply" buttons on job sites often open a new tab (target="_blank" / window.open).
    func webView(_ w: WKWebView, createWebViewWith config: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if action.targetFrame == nil, action.request.url != nil { w.load(action.request) }
        return nil
    }

    func load(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        didHopIntoEmbed = false
        webView.load(URLRequest(url: url))
    }

    /// Start Simplify-style autopilot (fill → Next → refill). Never submits.
    func startAutopilot() {
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
        driveState = .probing
        statusLine = "Resuming autopilot…"
        Theme.impact(.soft)
        webView.evaluateJavaScript("window.__applyDrive && window.__applyDrive({ mode: 'run' })")
    }

    /// One-shot fill on this frame + any ATS iframes (postMessage fan-out).
    func autofill() {
        Theme.impact(.soft)
        webView.evaluateJavaScript(
            "window.__applyAutofillAll ? window.__applyAutofillAll() : (window.__applyAutofill && window.__applyAutofill())")
    }
    func goBack() { if webView.canGoBack { webView.goBack() } }

    func webView(_ w: WKWebView, didStartProvisionalNavigation n: WKNavigation!) {
        loading = true
        currentURL = w.url?.absoluteString
        canGoBack = w.canGoBack
    }
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        loading = false
        currentURL = w.url?.absoluteString
        canGoBack = w.canGoBack
        hopIntoAtsEmbedIfNeeded(w) { [weak self] hopped in
            guard let self, !hopped else { return }
            // Soft probe so the banner updates after redirects (skip if we just
            // navigated into an embed — that load will probe on its own didFinish).
            w.evaluateJavaScript("window.__applyDrive && window.__applyDrive({ mode: 'probe' })")
            _ = self
        }
    }

    /// If this careers page only hosts the application inside an ATS iframe, navigate
    /// into that embed so fill/probe run against real `<input>`s in the main frame.
    private func hopIntoAtsEmbedIfNeeded(_ w: WKWebView, completion: @escaping (Bool) -> Void) {
        guard !didHopIntoEmbed else { completion(false); return }
        let js = """
        (function () {
          const re = /(?:boards|job-boards)\\.greenhouse\\.io\\/(?:embed\\/job_app|embed\\/job_board)|jobs\\.lever\\.co\\/|jobs\\.ashbyhq\\.com\\//i;
          for (const f of document.querySelectorAll('iframe[src]')) {
            try {
              const u = new URL(f.getAttribute('src'), location.href).href;
              if (re.test(u)) return u;
            } catch (e) {}
          }
          return null;
        })()
        """
        w.evaluateJavaScript(js) { [weak self] result, _ in
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
        // Only real fill reports update lastFill — probe/drive status pings also
        // carry filled:0 and would otherwise flash a false "no fields" toast.
        if status == "filled" {
            lastFill = (filled, essays, rules)
        }

        switch status {
        case "probe":
            if case .idle = driveState { /* keep idle until user starts */ }
            else if driveState == .probing { statusLine = "Scanning the page…" }
        case "filling":
            driveState = .filling
            statusLine = step > 0 ? "Filling step \(step + 1)…" : "Filling fields…"
        case "advancing":
            driveState = .advancing
            let detail = d["detail"] as? String
            statusLine = detail == "reveal" ? "Opening the application…" : "Advancing…"
            Theme.selection()
        case "needsHuman":
            let reason = (d["blocker"] as? String) ?? "Something needs you"
            driveState = .needsHuman(reason)
            statusLine = reason
            Theme.notify(.warning)
        case "watchingClear":
            driveState = .watchingClear
            statusLine = "Clear — resume autopilot when you’re ready"
            Theme.notify(.success)
        case "paused":
            driveState = .paused
            statusLine = "Paused — you can fill manually"
        case "ready":
            driveState = .ready
            statusLine = "Ready — review & submit yourself"
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
}

/// Bridges the model's WKWebView into SwiftUI.
struct WebViewContainer: UIViewRepresentable {
    let model: WebViewModel
    func makeUIView(context: Context) -> WKWebView { model.webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

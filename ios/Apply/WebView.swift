import SwiftUI
import WebKit

/// Owns the WKWebView for one application. The profile is injected on every page
/// load (so it survives multi-step navigation and reaches iframes); the native
/// ⚡ button calls `autofill()`.
final class WebViewModel: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    let webView: WKWebView
    @Published var loading = false
    @Published var lastFill: (filled: Int, essays: Int, rules: String)? = nil

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
    }

    // "Apply" buttons on job sites often open a new tab (target="_blank" / window.open).
    // A bare WKWebView has nowhere to put a new window, so it silently drops the nav —
    // the button looks dead. Load such requests in the same view instead.
    func webView(_ w: WKWebView, createWebViewWith config: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if action.targetFrame == nil, action.request.url != nil { w.load(action.request) }
        return nil
    }

    func load(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        webView.load(URLRequest(url: url))
    }

    /// Fire the autofill engine on the current page.
    func autofill() { webView.evaluateJavaScript("window.__applyAutofill && window.__applyAutofill()") }
    func goBack() { if webView.canGoBack { webView.goBack() } }

    // Navigation state for the toolbar.
    func webView(_ w: WKWebView, didStartProvisionalNavigation n: WKNavigation!) { loading = true }
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) { loading = false }
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { loading = false }

    // Result from window.webkit.messageHandlers.applyfill.postMessage(...).
    // `rules` says which rule set actually ran, so "bundled" in the debug line is a
    // visible signal that the served rules never arrived.
    func userContentController(_ u: WKUserContentController, didReceive m: WKScriptMessage) {
        guard m.name == "applyfill", let d = m.body as? [String: Any] else { return }
        lastFill = (d["filled"] as? Int ?? 0, d["essays"] as? Int ?? 0,
                    d["rules"] as? String ?? "?")
    }
}

/// Bridges the model's WKWebView into SwiftUI.
struct WebViewContainer: UIViewRepresentable {
    let model: WebViewModel
    func makeUIView(context: Context) -> WKWebView { model.webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

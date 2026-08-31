import XCTest

/// Drives the real app: taps through to the in-app apply browser, lets the probe
/// form self-run Autofill, and reads the verdict the page renders. The form is
/// served by the local stub backend at 127.0.0.1:8000.
final class AutofillProbeTests: XCTestCase {

    override func setUpWithError() throws { continueAfterFailure = true }

    func testAutofillProbe() throws {
        let app = XCUIApplication()
        app.launch()

        // Land on Apply with the staged match.
        let open = app.buttons["Open form"]
        XCTAssertTrue(open.waitForExistence(timeout: 30),
                      "‘Open form’ never appeared — app may not be signed in")
        print("PROBE: tapping Open form")
        open.tap()

        let web = app.webViews.firstMatch
        XCTAssertTrue(web.waitForExistence(timeout: 30), "no WKWebView appeared")
        print("PROBE: webview up")

        // The page polls for the injected engine, runs Fill, then renders a verdict.
        var verdict = ""
        let deadline = Date().addingTimeInterval(90)
        while Date() < deadline {
            let labels = web.staticTexts.allElementsBoundByIndex.map { $0.label }
            if let v = labels.first(where: {
                $0.contains("CHECKS PASSED") || $0.contains("FAILED")
                || $0.contains("never injected")
            }) { verdict = v; break }
            Thread.sleep(forTimeInterval: 2)
        }

        print("PROBE_VERDICT_BEGIN")
        print(verdict.isEmpty ? "(no verdict rendered)" : verdict)
        print("PROBE_VERDICT_END")

        // Full page text, so failures name the field.
        print("PROBE_PAGE_BEGIN")
        for t in web.staticTexts.allElementsBoundByIndex where !t.label.isEmpty {
            print("WV| \(t.label)")
        }
        print("PROBE_PAGE_END")

        // Hold the screen so an external screenshot catches the verdict.
        Thread.sleep(forTimeInterval: 12)

        XCTAssertTrue(verdict.contains("CHECKS PASSED"),
                      "autofill probe verdict was: \(verdict)")
    }
}

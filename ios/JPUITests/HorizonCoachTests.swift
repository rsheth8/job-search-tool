import XCTest

/// Drives the new "Ask Horizon" affordance on the onboarding quiz and prints
/// what it actually says, plus whether the answer came from Apple Intelligence
/// on-device or from the written fallback.
final class HorizonCoachTests: XCTestCase {

    var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = true
        app = XCUIApplication()
    }

    private func shoot(_ n: String) {
        let a = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        a.name = n; a.lifetime = .keepAlways; add(a)
    }

    private func sheetText() -> [String] {
        var out: [String] = []
        let n = min(app.staticTexts.count, 30)
        for i in 0..<n {
            let t = app.staticTexts.element(boundBy: i)
            guard t.exists, !t.label.isEmpty else { continue }
            out.append(t.label)
        }
        return out
    }

    func testAskHorizonOnQuizSteps() throws {
        // The launch sequence is 1.75s of cinematic before the first tappable
        // control exists. Tests are not the audience for it.
        app.launchArguments += ["-JobPilotSkipLaunch"]
        app.launch()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 30))
        Thread.sleep(forTimeInterval: 4)

        let dev = app.buttons["Dev sign-in (simulator)"]
        if dev.waitForExistence(timeout: 8) { dev.tap(); Thread.sleep(forTimeInterval: 6) }

        let ask = app.buttons["Ask Horizon about this step"]
        XCTAssertTrue(ask.waitForExistence(timeout: 20), "no Ask Horizon button on the quiz")

        for round in 1...3 {
            print("\n===== ASK ROUND \(round) =====")
            let stepLbl = app.staticTexts.matching(
                NSPredicate(format: "label CONTAINS ' OF '")).firstMatch
            print("STEP| \(stepLbl.exists ? stepLbl.label : "?")")

            ask.tap()
            Thread.sleep(forTimeInterval: 3)
            // on-device generation can take a few seconds
            var settled = false
            for _ in 0..<10 {
                let texts = sheetText()
                if !texts.contains(where: { $0.contains("Horizon is thinking") }) {
                    settled = true; break
                }
                Thread.sleep(forTimeInterval: 2)
            }
            print("SETTLED| \(settled)")
            shoot("horizon-\(round)")
            for t in sheetText() { print("SHEET| \(t)") }

            let done = app.buttons["Close Horizon"]
            if done.exists && done.isHittable { done.tap() } else {
                app.buttons["Done"].firstMatch.tap()
            }
            Thread.sleep(forTimeInterval: 2)

            // move to the next step so the next ask is a different context
            let cont = app.buttons["Continue"]
            if cont.exists && cont.isHittable && cont.isEnabled {
                cont.tap(); Thread.sleep(forTimeInterval: 2)
            } else if app.buttons["Get started"].exists {
                app.buttons["Get started"].tap(); Thread.sleep(forTimeInterval: 2)
            } else if app.buttons["Skip"].exists && app.buttons["Skip"].isHittable {
                app.buttons["Skip"].tap(); Thread.sleep(forTimeInterval: 2)
            }
        }
    }
}

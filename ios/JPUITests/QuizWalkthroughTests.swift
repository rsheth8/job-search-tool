import XCTest

/// Walks the 13-step onboarding quiz through the UI, typing the resume's real
/// values. All data entry happens in the app; nothing is written server-side by
/// the test. Unknown placeholders are printed rather than guessed.
final class QuizWalkthroughTests: XCTestCase {

    var app: XCUIApplication!

    /// EXACT placeholder -> value. Substring matching is a trap here:
    /// "Add a skill" contains "il", which a loose matcher fills with a state code.
    let answers: [String: String] = [
        "Ada": "Rahil",
        "Lovelace": "Sheth",
        "you@school.edu": "rahil.sheth@example.com",
        "555-0100": "(312) 555-0147",
        "Chicago": "Chicago",
        "IL": "IL",
        "60601": "60601",
        "United States": "United States",
        "https://linkedin.com/in/…": "https://linkedin.com/in/rahilsheth",
        "https://github.com/…": "https://github.com/rahilsheth",
        "Add a role": "platform engineer",
        "Add a city or Remote": "Remote",
        "Add a skill": "distributed systems",
    ]

    override func setUpWithError() throws {
        continueAfterFailure = true
        app = XCUIApplication()
    }

    private func shoot(_ label: String) {
        let att = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        att.name = label
        att.lifetime = .keepAlways
        add(att)
    }

    private func stepLabel() -> String {
        let e = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS ' OF '")).firstMatch
        return e.exists ? e.label : "?"
    }

    private func dismissKeyboard() {
        if app.keyboards.count > 0 {
            // "return" commits chip entry; Done/dismiss also closes the keyboard.
            for key in ["return", "Return", "done", "Done"] {
                let k = app.keyboards.buttons[key]
                if k.exists && k.isHittable { k.tap(); break }
            }
        }
        Thread.sleep(forTimeInterval: 1)
    }

    func testWalkQuiz() throws {
        app.launch()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 30))
        Thread.sleep(forTimeInterval: 5)

        let dev = app.buttons["Dev sign-in (simulator)"]
        if dev.waitForExistence(timeout: 6) { dev.tap(); Thread.sleep(forTimeInterval: 6) }

        if app.buttons["Get started"].waitForExistence(timeout: 15) {
            print("STEP: Get started")
            app.buttons["Get started"].tap()
            Thread.sleep(forTimeInterval: 3)
        }

        var lastStep = ""
        for pass in 1...16 {
            Thread.sleep(forTimeInterval: 2)
            let step = stepLabel()
            print("\n----- PASS \(pass) | \(step) -----")
            shoot("q\(pass)")

            let sn = min(app.staticTexts.count, 10)
            for i in 0..<sn {
                let t = app.staticTexts.element(boundBy: i)
                guard t.exists else { continue }
                if t.label.count > 8 { print("HEAD| \(t.label)") }
            }

            // Fill recognised text fields.
            let n = app.textFields.count
            print("FIELDS| count=\(n)")
            for i in 0..<n {
                let f = app.textFields.element(boundBy: i)
                guard f.exists else { continue }
                let ph = f.placeholderValue ?? ""
                let cur = (f.value as? String) ?? ""
                let empty = cur.isEmpty || cur == ph
                print("FLD| ph='\(ph)' value='\(cur)' empty=\(empty)")
                guard empty, let v = answers[ph] else {
                    if empty && !ph.isEmpty { print("     UNKNOWN placeholder, left blank") }
                    continue
                }
                f.tap(); Thread.sleep(forTimeInterval: 1)
                f.typeText(v)
                print("     typed -> \(v)")
                dismissKeyboard()
            }
            dismissKeyboard()

            print("BUTTONS|")
            let bn = min(app.buttons.count, 30)
            for i in 0..<bn {
                let b = app.buttons.element(boundBy: i)
                guard b.exists, !b.label.isEmpty else { continue }
                print("BTN| \(b.label) | sel=\(b.isSelected) hit=\(b.isHittable)")
            }

            // Advance. Only the LAST step's Done ends the quiz -- the keyboard has
            // a "Done" key too, and tapping that ended the run at step 2.
            let isFinal = step.contains("13 OF 13")
            let done = app.buttons["Done"]
            let cont = app.buttons["Continue"]
            if isFinal, done.exists, done.isHittable {
                print("STEP: Done (final)")
                done.tap(); Thread.sleep(forTimeInterval: 5)
                shoot("q-final")
                break
            }
            if cont.exists, cont.isHittable, cont.isEnabled {
                print("STEP: Continue")
                cont.tap()
            } else if app.buttons["Skip"].exists, app.buttons["Skip"].isHittable {
                print("STEP: Skip (Continue unavailable)")
                app.buttons["Skip"].tap()
            } else {
                print("STEP: stuck at \(step)")
                break
            }
            if step == lastStep && pass > 2 {
                print("STEP: not advancing past \(step); stopping")
                break
            }
            lastStep = step
        }
        shoot("q-end")
    }
}

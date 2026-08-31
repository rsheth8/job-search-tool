import XCTest

/// The whole new-user run, entirely through the app's UI:
///   dev sign-in -> import resumes/swe.pdf via the document picker -> 13-step quiz.
///
/// Entry is keyed on the "N OF 13" step label, not on placeholders. Placeholder
/// matching is unsafe here: the City field's placeholder is literally "Chicago",
/// and XCUITest reports the placeholder as `value` when a field is empty, so an
/// imported "Chicago" is indistinguishable from an empty box -- typing again
/// produced "ChicagoChicago".
final class FullNewUserTests: XCTestCase {

    var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = true
        app = XCUIApplication()
    }

    private func shoot(_ n: String) {
        let a = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        a.name = n; a.lifetime = .keepAlways; add(a)
    }

    private func step() -> String {
        let e = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS ' OF '")).firstMatch
        return e.exists ? e.label : "?"
    }

    private func dismissKeyboard() {
        if app.keyboards.count > 0 {
            for k in ["return", "Return", "done", "Done"] {
                let b = app.keyboards.buttons[k]
                if b.exists && b.isHittable { b.tap(); break }
            }
        }
        Thread.sleep(forTimeInterval: 1)
    }

    /// Type into the field at `idx`, only when it is genuinely empty.
    private func type(_ idx: Int, _ text: String, _ why: String) {
        let f = app.textFields.element(boundBy: idx)
        guard f.exists else { print("  no field \(idx) (\(why))"); return }
        let ph = f.placeholderValue ?? ""
        let cur = (f.value as? String) ?? ""
        if !cur.isEmpty && cur != ph {
            print("  field \(idx) already '\(cur)' — leaving (\(why))")
            return
        }
        f.tap(); Thread.sleep(forTimeInterval: 1)
        f.typeText(text)
        print("  field \(idx) typed '\(text)' (\(why))")
        dismissKeyboard()
    }

    private func chip(_ label: String) {
        let b = app.buttons[label]
        if b.exists && b.isHittable {
            b.tap(); print("  tapped chip '\(label)'")
            Thread.sleep(forTimeInterval: 1)
        } else {
            print("  chip '\(label)' not found")
        }
    }

    func testFullNewUserRun() throws {
        app.launch()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 30))
        Thread.sleep(forTimeInterval: 5)
        shoot("00-launch")

        // --- sign in through the app ---
        let dev = app.buttons["Dev sign-in (simulator)"]
        XCTAssertTrue(dev.waitForExistence(timeout: 15),
                      "expected the sign-in gate for a new user")
        print("STEP: Dev sign-in")
        dev.tap()
        Thread.sleep(forTimeInterval: 7)
        shoot("01-signed-in")

        // --- import the resume through the app's own picker ---
        let upload = app.buttons.containing(
            NSPredicate(format: "label CONTAINS 'Upload a resume'")).firstMatch
        XCTAssertTrue(upload.waitForExistence(timeout: 20), "no resume upload button")
        print("STEP: Upload a resume")
        upload.tap()
        Thread.sleep(forTimeInterval: 5)

        func tapFirst(_ needle: String, _ why: String, _ t: TimeInterval = 8) -> Bool {
            let p = NSPredicate(format: "label CONTAINS %@", needle)
            let end = Date().addingTimeInterval(t)
            while Date() < end {
                for q in [app.cells, app.buttons, app.staticTexts, app.otherElements] {
                    let e = q.matching(p).firstMatch
                    if e.exists && e.isHittable {
                        print("STEP: tap '\(needle)' (\(why))"); e.tap()
                        Thread.sleep(forTimeInterval: 3); return true
                    }
                }
                Thread.sleep(forTimeInterval: 1)
            }
            print("STEP: no '\(needle)' (\(why))"); return false
        }
        var opened = tapFirst("swe", "recents", 4)
        if !opened {
            _ = tapFirst("Browse", "locations", 6)
            _ = tapFirst("On My iPhone", "local", 8)
            _ = tapFirst("JobPilot", "app documents", 8)
            opened = tapFirst("swe", "the pdf", 8)
        }
        XCTAssertTrue(opened, "could not open swe.pdf")
        Thread.sleep(forTimeInterval: 14)
        shoot("02-imported")
        let card = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS 'Read from your resume'")).firstMatch
        print("IMPORT| \(card.exists ? card.label : "(no card)")")

        // --- walk the quiz ---
        if app.buttons["Get started"].waitForExistence(timeout: 12) {
            print("STEP: Get started"); app.buttons["Get started"].tap()
            Thread.sleep(forTimeInterval: 3)
        }

        var last = ""
        for pass in 1...16 {
            Thread.sleep(forTimeInterval: 2)
            let s = step()
            print("\n----- \(s) -----")
            shoot("q-\(s.replacingOccurrences(of: " ", with: ""))")

            switch true {
            case s == "2 OF 13":        // what are you looking for
                type(0, "platform engineer", "role")
                type(1, "Remote", "location")
                type(2, "distributed systems", "skill")
            case s == "4 OF 13":        // where you live (city/state came from resume)
                type(2, "60601", "zip")
                type(4, "United States", "country")
            case s == "7 OF 13":        // work and authorization
                type(0, "6", "years of experience")
                type(1, "Acme Corp", "current company")
                type(2, "Senior Platform Engineer", "current title")
            case s == "8 OF 13":        // start date and setup
                chip("Remote"); chip("2 weeks")
            case s == "9 OF 13":        // usual application answers
                chip("Job board")
            case s == "10 OF 13":       // what they can cite
                chip("Fill from my profile")
            default:
                print("  (nothing to enter)")
            }
            dismissKeyboard()

            if s == "13 OF 13" {
                let d = app.buttons["Done"]
                if d.exists && d.isHittable {
                    print("STEP: Done"); d.tap(); Thread.sleep(forTimeInterval: 6)
                    shoot("q-final"); break
                }
            }
            let cont = app.buttons["Continue"]
            if cont.exists && cont.isHittable && cont.isEnabled {
                print("STEP: Continue"); cont.tap()
            } else if app.buttons["Skip"].exists && app.buttons["Skip"].isHittable {
                print("STEP: Skip"); app.buttons["Skip"].tap()
            } else {
                print("STEP: stuck at \(s)"); break
            }
            if s == last && pass > 2 { print("STEP: no progress past \(s)"); break }
            last = s
        }
        shoot("03-end")
    }
}

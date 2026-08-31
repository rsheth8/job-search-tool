import XCTest

/// New-user walkthrough driven entirely through the app's UI.
/// Signs in, then imports resumes/swe.pdf via the app's own document picker.
/// Bulk element enumeration is avoided: querying every element while the picker
/// animates races with the snapshot and aborts the test.
final class OnboardingTests: XCTestCase {

    var app: XCUIApplication!

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

    /// First hittable match for a label substring, across the common types.
    private func find(_ needle: String, timeout: TimeInterval = 8) -> XCUIElement? {
        let pred = NSPredicate(format: "label CONTAINS %@", needle)
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            for q in [app.cells, app.buttons, app.staticTexts,
                      app.otherElements, app.images] {
                let e = q.matching(pred).firstMatch
                if e.exists && e.isHittable { return e }
            }
            Thread.sleep(forTimeInterval: 1)
        }
        return nil
    }

    @discardableResult
    private func tap(_ needle: String, _ why: String,
                     timeout: TimeInterval = 8) -> Bool {
        guard let e = find(needle, timeout: timeout) else {
            print("STEP: no match for '\(needle)' (\(why))")
            return false
        }
        print("STEP: tapping '\(needle)' — \(why)")
        e.tap()
        Thread.sleep(forTimeInterval: 3)
        return true
    }

    func testNewUserResumeImport() throws {
        // The launch sequence is 1.75s of cinematic before the first tappable
        // control exists. Tests are not the audience for it.
        app.launchArguments += ["-JobPilotSkipLaunch"]
        app.launch()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 30))
        Thread.sleep(forTimeInterval: 4)
        shoot("01-launch")

        // 1. Sign in through the app.
        if tap("Dev sign-in", "simulator sign-in", timeout: 10) {
            Thread.sleep(forTimeInterval: 5)
            shoot("02-signed-in")
        }

        // 2. Welcome step -> resume upload.
        XCTAssertTrue(tap("Upload a resume", "open document picker", timeout: 20),
                      "no 'Upload a resume' button on the welcome step")
        Thread.sleep(forTimeInterval: 4)
        shoot("03-picker-open")

        // 3. Navigate the picker to the app's Documents folder and open swe.pdf.
        //    Recents is empty on a fresh sim, so go through Browse.
        var opened = tap("swe", "swe.pdf in Recents", timeout: 4)
        if !opened {
            tap("Browse", "picker locations", timeout: 6)
            shoot("04-browse")
            tap("On My iPhone", "local storage", timeout: 8)
            shoot("05-on-my-iphone")
            tap("JobPilot", "app's exposed Documents", timeout: 8)
            shoot("06-jobpilot-folder")
            opened = tap("swe", "swe.pdf", timeout: 8)
        }
        print("STEP: opened swe.pdf = \(opened)")
        shoot("07-after-tap")

        // 4. Upload + real parse take a moment.
        Thread.sleep(forTimeInterval: 15)
        shoot("08-after-import")

        // The success card names what got filled.
        let card = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS 'Read from your resume' OR label CONTAINS 'filled'")
        ).firstMatch
        if card.waitForExistence(timeout: 10) {
            print("IMPORT_RESULT| \(card.label)")
        } else {
            print("IMPORT_RESULT| (no success card found)")
        }
        XCTAssertTrue(opened, "never opened swe.pdf in the picker")
    }
}

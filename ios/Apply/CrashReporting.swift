import Foundation

/// Client crash reporting. Backend Sentry covers API errors; this starts
/// sentry-cocoa when the package is linked and ``SentryDSN`` is set in Info.plist.
///
/// TestFlight: add the Sentry Cocoa SDK (see ios/README) and put the DSN in
/// Info.plist. Until then this is a no-op so the app still builds.
enum CrashReporting {
    static func start() {
        let dsn = (Bundle.main.object(forInfoDictionaryKey: "SentryDSN") as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !dsn.isEmpty else { return }
        guard let sentry = NSClassFromString("SentrySDK") as? NSObject.Type else { return }
        let options = ["dsn": dsn] as NSDictionary
        let sel = NSSelectorFromString("startWithOptions:")
        if sentry.responds(to: sel) {
            _ = sentry.perform(sel, with: options)
        }
    }
}

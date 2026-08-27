import SwiftUI
import UserNotifications

/// Push registration and tap handling.
///
/// Two moments are worth interrupting you for: new matches landed, and the worker
/// finished filling a form and needs your approval. Tapping either should drop you
/// on the screen that answers it, not the last tab you happened to be on.
///
/// Permission is requested lazily — on first use of the Settings toggle, not at
/// launch — because a permission prompt before the app has shown you anything is
/// the fastest way to get denied permanently.
@MainActor
final class PushManager: NSObject, ObservableObject {
    static let shared = PushManager()

    /// Which tab to show. Set from a notification tap; `RootView` binds to it.
    @Published var selectedTab: Int = 0
    @Published var authorized = false
    /// False when the server has no APNs credentials — the app says so rather than
    /// registering into a void.
    @Published var serverConfigured = true
    @Published var lastError: String?

    private var pendingToken: String?

    func refreshAuthorization() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        authorized = settings.authorizationStatus == .authorized
    }

    /// Ask for permission, then register with APNs. Safe to call repeatedly.
    func enable() async {
        do {
            let granted = try await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .sound, .badge])
            authorized = granted
            guard granted else { return }
            UIApplication.shared.registerForRemoteNotifications()
        } catch {
            lastError = "\(error)"
        }
    }

    /// Called by the app delegate once APNs hands us a token.
    func register(deviceToken: Data, config: Config) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        pendingToken = token
        Task {
            do {
                let configured = try await APIClient(config: config)
                    .registerDevice(token: token)
                serverConfigured = configured
            } catch {
                lastError = "\(error)"
            }
        }
    }

    /// Re-send the token after the backend URL or user changes in Settings.
    func reregister(config: Config) {
        guard let token = pendingToken else { return }
        Task { _ = try? await APIClient(config: config).registerDevice(token: token) }
    }

    /// Route a tap to the tab that answers it.
    private func open(kind: String) {
        switch kind {
        case "preview": selectedTab = 1     // In flight — something awaits approval
        case "chat": selectedTab = 3        // Chat — reminder / digest
        default: selectedTab = 0            // Apply — new matches to look at
        }
    }
}

extension PushManager: UNUserNotificationCenterDelegate {
    /// Show the banner even while the app is open — you may be mid-application on
    /// another posting when one finishes filling.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let kind = response.notification.request.content.userInfo["kind"] as? String ?? ""
        await MainActor.run { PushManager.shared.open(kind: kind) }
    }
}

/// Minimal app delegate — SwiftUI has no hook for the APNs token callbacks.
final class AppDelegate: NSObject, UIApplicationDelegate {
    /// The notification delegate must be assigned **before launch finishes**, or
    /// taps are silently dropped: iOS resolves the delegate at launch, so setting it
    /// later (e.g. once permission is granted) leaves the banner dismissing without
    /// ever routing. Found by tapping a real notification in the simulator.
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = PushManager.shared
        return true
    }

    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
        Task { @MainActor in PushManager.shared.register(deviceToken: token,
                                                         config: Config.shared) }
    }

    func application(_ application: UIApplication,
                     didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in PushManager.shared.lastError = "\(error)" }
    }
}

import SwiftUI
import UserNotifications

/// Push registration and tap handling.
///
/// Worth interrupting for: new matches landed, and chat/reminder digests.
/// Tapping should drop you on the screen that answers it.
///
/// Permission is requested lazily — on first use of the Settings toggle, not at
/// launch — because a permission prompt before the app has shown you anything is
/// the fastest way to get denied permanently.
@MainActor
final class PushManager: NSObject, ObservableObject {
    static let shared = PushManager()

    /// Which tab to show. Set from a notification tap; `RootView` binds to it.
    @Published var selectedTab: Int = 0
    /// True when the new tab is to the right of the old one (Apply → Settings).
    @Published private(set) var tabForward = true
    /// Horizon handoffs use a slower settle than dock taps.
    @Published private(set) var tabFromHorizon = false
    /// Screen-level hop after a confirmed Ask handoff (sheet, pane, job, quiz).
    @Published var hop: HorizonHop?
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
            lastError = APIClient.userMessage(for: error)
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
                lastError = APIClient.userMessage(for: error)
            }
        }
    }

    /// Re-send the token after the backend URL or user changes in Settings.
    func reregister(config: Config) {
        guard let token = pendingToken else { return }
        Task { _ = try? await APIClient(config: config).registerDevice(token: token) }
    }

    /// Dock binding — records slide direction so RootView can animate the page.
    var tabSelection: Binding<Int> {
        Binding(
            get: { self.selectedTab },
            set: { self.selectTab($0, fromHorizon: false) }
        )
    }

    func selectTab(_ index: Int, fromHorizon: Bool = false) {
        guard index != selectedTab else { return }
        tabForward = index > selectedTab
        tabFromHorizon = fromHorizon
        selectedTab = index
    }

    /// Route a tap or in-app deep link to the tab (and screen) that answers it.
    /// Tabs: `apply` / `you` / `chat` / `settings`. Screens: `apply:filed`,
    /// `you:identity`, `job:N`, `setup`, `settings:quiz`, …
    func openDeepLink(_ link: String, fromHorizon: Bool = false) {
        let parts = link.split(separator: ":").map { String($0).lowercased() }
        let head = parts.first ?? link.lowercased()
        let tail = parts.dropFirst().first

        hop = HorizonHop.parse(head: head, tail: tail)

        if head == "setup" || (head == "quiz" && tail == nil) {
            return
        }
        if head == "chat" || head == "ask" || head == "assistant" {
            selectTab(2, fromHorizon: fromHorizon)
            return
        }
        let index: Int
        switch head {
        case "you", "about", "identity", "profile": index = 1
        case "settings": index = 3
        default: index = 0
        }
        if selectedTab == index {
            // Same tab — views watch `hop` and still consume it.
            tabFromHorizon = fromHorizon
            return
        }
        selectTab(index, fromHorizon: fromHorizon)
    }

    /// Route a tap to the tab that answers it.
    fileprivate func open(kind: String, fromHorizon: Bool = false) {
        openDeepLink(kind, fromHorizon: fromHorizon)
    }
}

/// In-app destination beyond a dock tab. Ask confirms before this is set.
enum HorizonHop: Equatable {
    case applyFiled
    case applyJob(Int)
    case youIdentity
    case youSearch
    case youAdd
    case youProjects
    case youExperience
    case youImport
    case settingsNotifications
    case settingsFeedback
    case settingsQuiz
    case setup

    static func parse(head: String, tail: String?) -> HorizonHop? {
        switch (head, tail) {
        case ("apply", "filed"): return .applyFiled
        case ("job", let id?) where Int(id) != nil:
            return .applyJob(Int(id)!)
        case ("you", "identity"), ("identity", _): return .youIdentity
        case ("you", "search"): return .youSearch
        case ("you", "add"): return .youAdd
        case ("you", "projects"): return .youProjects
        case ("you", "experience"): return .youExperience
        case ("you", "import"): return .youImport
        case ("settings", "notifications"): return .settingsNotifications
        case ("settings", "feedback"): return .settingsFeedback
        case ("settings", "quiz"): return .settingsQuiz
        case ("setup", _), ("quiz", nil): return .setup
        default: return nil
        }
    }
}

extension PushManager: UNUserNotificationCenterDelegate {
    /// Show the banner even while the app is open — you may be mid-application on
    /// another posting when a digest arrives.
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
        await MainActor.run { PushManager.shared.open(kind: kind, fromHorizon: false) }
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
        let fog = UIColor(red: 242 / 255, green: 245 / 255, blue: 248 / 255, alpha: 1)
        UIWindow.appearance().backgroundColor = fog
        return true
    }

    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
        Task { @MainActor in PushManager.shared.register(deviceToken: token,
                                                         config: Config.shared) }
    }

    func application(_ application: UIApplication,
                     didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in
            PushManager.shared.lastError = APIClient.userMessage(for: error)
        }
    }
}

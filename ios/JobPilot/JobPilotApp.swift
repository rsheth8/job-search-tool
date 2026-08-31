import SwiftUI

@main
struct JobPilotApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var config = Config.shared
    @StateObject private var push = PushManager.shared
    @StateObject private var chrome = AppChrome.shared
    @StateObject private var auth = AuthManager.shared
    @StateObject private var setup = SetupGate.shared

    /// The launch sequence doubles as cover for the session check: `auth.refresh()`
    /// runs underneath it, so a returning tester never sees the sign-in screen
    /// flash before their session resolves.
    @State private var launching = !ProcessInfo.processInfo.arguments.contains("-JobPilotSkipLaunch")

    var body: some Scene {
        WindowGroup {
            ZStack {
                Group {
                    if auth.isSignedIn {
                        if setup.needsSetup {
                            SetupView()
                        } else {
                            RootView()
                        }
                    } else {
                        SignInView()
                    }
                }
                .opacity(launching ? 0 : 1)

                if launching {
                    LaunchView { withAnimation(.easeOut(duration: 0.35)) { launching = false } }
                        .transition(.opacity)
                        .zIndex(2)
                }
            }
            .environmentObject(config)
            .environmentObject(push)
            .environmentObject(chrome)
            .environmentObject(auth)
            .environmentObject(setup)
            .tint(Theme.accent)
            .preferredColorScheme(.light)
            .background { AmbientBackground() }
            .task {
                #if DEBUG
                Self.applyDebugLaunchArgs()
                #endif
                CrashReporting.start()
                await auth.refresh()
                await setup.refresh(config: config)
                await push.refreshAuthorization()
            }
            .onChange(of: auth.isSignedIn) { _, signedIn in
                Task {
                    if signedIn {
                        await setup.refresh(config: config)
                    } else {
                        setup.needsSetup = false
                    }
                }
            }
            .onChange(of: push.hop) { _, hop in
                if hop == .setup {
                    push.hop = nil
                    setup.reopen()
                }
            }
        }
    }

    #if DEBUG
    /// Simulator walk-through: `simctl launch … com.rahil.jobpilot -JobPilotTab 1`
    private static func applyDebugLaunchArgs() {
        let args = ProcessInfo.processInfo.arguments
        if let i = args.firstIndex(of: "-JobPilotTab"),
           i + 1 < args.count,
           let n = Int(args[i + 1]),
           (0...3).contains(n) {
            PushManager.shared.selectedTab = n
        }
        if args.contains("-JobPilotQuiz") {
            PushManager.shared.selectedTab = 3
            PushManager.shared.hop = .settingsQuiz
        }
    }
    #endif
}

/// Custom floating dock instead of stock TabView chrome.
/// Tabs: Apply · You · Ask · Settings.
struct RootView: View {
    @EnvironmentObject var push: PushManager
    @EnvironmentObject var chrome: AppChrome
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack(alignment: .bottom) {
            AmbientBackground()

            Group {
                switch push.selectedTab {
                case 0: QueueView()
                case 1: KnowledgeView()
                case 2: ChatView()
                default: SettingsView()
                }
            }
            .id(push.selectedTab)
            .transition(tabTransition)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .clipped()

            if !chrome.dockHidden {
                FloatingTabBar(
                    selection: push.tabSelection,
                    readyBadge: chrome.readyCount
                )
                .transition(.move(edge: .bottom).combined(with: .opacity))
                .zIndex(1)
            }
        }
        .animation(reduceMotion ? nil : Theme.springSoft, value: chrome.dockHidden)
        .animation(reduceMotion ? nil : tabMotion, value: push.selectedTab)
        .ignoresSafeArea(.keyboard)
        .onChange(of: push.selectedTab) { _, _ in
            chrome.dockHidden = false
        }
    }

    private var tabMotion: Animation {
        push.tabFromHorizon ? Theme.springHorizon : Theme.springSoft
    }

    private var tabTransition: AnyTransition {
        if reduceMotion { return .opacity }
        let incoming: Edge = push.tabForward ? .trailing : .leading
        let outgoing: Edge = push.tabForward ? .leading : .trailing
        return .asymmetric(
            insertion: .move(edge: incoming).combined(with: .opacity),
            removal: .move(edge: outgoing).combined(with: .opacity)
        )
    }
}

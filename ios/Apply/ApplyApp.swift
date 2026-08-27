import SwiftUI

@main
struct ApplyApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var config = Config.shared
    @StateObject private var push = PushManager.shared
    @StateObject private var chrome = AppChrome.shared
    @StateObject private var auth = AuthManager.shared
    @StateObject private var setup = SetupGate.shared

    var body: some Scene {
        WindowGroup {
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
            .environmentObject(config)
            .environmentObject(push)
            .environmentObject(chrome)
            .environmentObject(auth)
            .environmentObject(setup)
            .tint(Theme.accent)
            .preferredColorScheme(.light)
            .task {
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
        }
    }
}

/// Custom floating dock instead of stock TabView chrome.
/// Tabs: Apply · In flight · About · Chat · Settings (Chat is secondary).
struct RootView: View {
    @EnvironmentObject var push: PushManager
    @EnvironmentObject var chrome: AppChrome

    var body: some View {
        ZStack(alignment: .bottom) {
            Group {
                switch push.selectedTab {
                case 0: QueueView()
                case 1: InFlightView()
                case 2: KnowledgeView()
                case 3: ChatView()
                default: SettingsView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            if !chrome.dockHidden {
                FloatingTabBar(
                    selection: $push.selectedTab,
                    readyBadge: chrome.readyCount,
                    awaitingBadge: chrome.awaitingCount
                )
                .transition(.move(edge: .bottom).combined(with: .opacity))
                // Keep the dock above the apply Autofill inset so a failed hide
                // can't paint both on top of each other as a green blur.
                .zIndex(1)
            }
        }
        .animation(Theme.springSoft, value: chrome.dockHidden)
        .ignoresSafeArea(.keyboard)
        .onChange(of: push.selectedTab) { _, _ in
            // Leaving Apply detail via tab switch should always restore the dock;
            // QueueView.onAppear re-hides it if a detail is still pushed.
            chrome.dockHidden = false
        }
    }
}

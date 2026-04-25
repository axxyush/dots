import SwiftUI
import RoomPlan
import AVFoundation
import UIKit

private enum DotsIntroPhase: Int {
    case hidden
    case centeredWordmark
    case cornerWordmark
    case firstTyping
    case firstDeleting
    case secondTyping
    case secondDeleting
    case actionsVisible
}

struct ContentView: View {
    @State private var capturedRoom: CapturedRoom?
    @State private var scanDuration: TimeInterval = 0
    @State private var isScanning = false
    @State private var showFloorPlanUpload = false

    @State private var introPhase: DotsIntroPhase = .hidden
    @State private var typedLine = ""
    @State private var introTask: Task<Void, Never>?
    @State private var hasPlayedIntro = false
    @State private var scanIssue: ScanIssue?

    @StateObject private var scanManager = ScanManager()

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        if let room = capturedRoom {
            ResultsView(capturedRoom: room, scanDuration: scanDuration, scanManager: scanManager) {
                capturedRoom = nil
                isScanning = false
                scanManager.capturedPhotos.removeAll()
            }
        } else if isScanning {
            ScanningView(
                onComplete: { room, duration in
                    capturedRoom = room
                    scanDuration = duration
                },
                onCancel: {
                    isScanning = false
                    scanManager.capturedPhotos.removeAll()
                },
                scanManager: scanManager
            )
        } else if showFloorPlanUpload {
            FloorPlanUploadView(onDismiss: {
                showFloorPlanUpload = false
            })
        } else {
            introScreen
                .task { await startIntroIfNeeded() }
                .onDisappear { introTask?.cancel() }
        }
    }

    private var introScreen: some View {
        ZStack {
            DotsTheme.background.ignoresSafeArea()

            if introPhase.rawValue >= DotsIntroPhase.centeredWordmark.rawValue {
                DotsWordmark(
                    textSize: introPhase.rawValue >= DotsIntroPhase.cornerWordmark.rawValue ? 28 : 50,
                    dotDiameter: introPhase.rawValue >= DotsIntroPhase.cornerWordmark.rawValue ? 8 : 14,
                    weight: .semibold
                )
                .frame(
                    maxWidth: .infinity,
                    maxHeight: .infinity,
                    alignment: introPhase.rawValue >= DotsIntroPhase.cornerWordmark.rawValue ? .topTrailing : .center
                )
                .padding(introPhase.rawValue >= DotsIntroPhase.cornerWordmark.rawValue ? 24 : 0)
                .animation(.easeInOut(duration: reduceMotion ? 0.16 : 0.45), value: introPhase)
            }

            VStack {
                Spacer()

                if introPhase.rawValue >= DotsIntroPhase.firstTyping.rawValue {
                    Text(typedDisplayText)
                        .font(.system(size: 34, weight: .semibold, design: .rounded))
                        .foregroundStyle(DotsTheme.primaryText)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                        .frame(height: 102)
                        .lineLimit(2)
                        .padding(.horizontal, 12)
                } else {
                    Color.clear
                        .frame(height: 102)
                }

                Spacer()

                if introPhase == .actionsVisible {
                    VStack(spacing: 14) {
                        Button {
                            Task { await startScanFlow() }
                        } label: {
                            HStack {
                                Image(systemName: "viewfinder")
                                Text("Scan")
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(DotsPrimaryButtonStyle())

                        Button {
                            showFloorPlanUpload = true
                        } label: {
                            HStack {
                                Image(systemName: "square.and.arrow.up")
                                Text("Upload Floor Plan")
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(DotsSecondaryButtonStyle())
                    }
                    .padding(.horizontal, 24)
                    .padding(.bottom, 28)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
        }
        .preferredColorScheme(.dark)
        .alert(
            scanIssue?.title ?? "Scan Unavailable",
            isPresented: Binding(
                get: { scanIssue != nil },
                set: { if !$0 { scanIssue = nil } }
            ),
            actions: {
                if scanIssue == .cameraDenied {
                    Button("Open Settings") {
                        guard let settingsURL = URL(string: UIApplication.openSettingsURLString) else { return }
                        UIApplication.shared.open(settingsURL)
                    }
                }
                Button("OK", role: .cancel) { scanIssue = nil }
            },
            message: {
                Text(scanIssue?.message ?? "")
            }
        )
    }

    private var typedDisplayText: String {
        let isTypingPhase = introPhase == .firstTyping || introPhase == .secondTyping
        return isTypingPhase ? typedLine + "|" : typedLine
    }

    @MainActor
    private func startIntroIfNeeded() async {
        introTask?.cancel()

        if hasPlayedIntro {
            typedLine = ""
            introPhase = .actionsVisible
            return
        }

        introTask = Task { await runIntroSequence() }
    }

    @MainActor
    private func runIntroSequence() async {
        if reduceMotion {
            withAnimation(.easeOut(duration: 0.16)) {
                introPhase = .centeredWordmark
            }
            await pause(seconds: 0.25)

            withAnimation(.easeOut(duration: 0.16)) {
                introPhase = .cornerWordmark
            }
            introPhase = .firstTyping
            typedLine = "7 million Americans are blind"
            await pause(seconds: 0.6)

            introPhase = .firstDeleting
            typedLine = ""
            await pause(seconds: 0.2)

            introPhase = .secondTyping
            typedLine = "you are making them feel seen"
            await pause(seconds: 0.6)

            introPhase = .secondDeleting
            typedLine = ""
            await pause(seconds: 0.2)

            withAnimation(.easeOut(duration: 0.16)) {
                introPhase = .actionsVisible
            }
            hasPlayedIntro = true
            return
        }

        withAnimation(.easeOut(duration: 0.5)) {
            introPhase = .centeredWordmark
        }
        await pause(seconds: 0.9)

        withAnimation(.spring(response: 0.55, dampingFraction: 0.88)) {
            introPhase = .cornerWordmark
        }
        await pause(seconds: 0.35)

        introPhase = .firstTyping
        await typeLine("7 million Americans are blind", charDelayMs: 52)
        await pause(seconds: 0.7)

        introPhase = .firstDeleting
        await eraseLine(charDelayMs: 30)
        await pause(seconds: 0.22)

        introPhase = .secondTyping
        await typeLine("you are making them feel seen", charDelayMs: 52)
        await pause(seconds: 0.7)

        introPhase = .secondDeleting
        await eraseLine(charDelayMs: 28)
        await pause(seconds: 0.28)

        withAnimation(.easeOut(duration: 0.35)) {
            introPhase = .actionsVisible
        }
        hasPlayedIntro = true
    }

    @MainActor
    private func typeLine(_ text: String, charDelayMs: UInt64) async {
        typedLine = ""
        for character in text {
            if Task.isCancelled { return }
            typedLine.append(character)
            try? await Task.sleep(nanoseconds: charDelayMs * 1_000_000)
        }
    }

    @MainActor
    private func eraseLine(charDelayMs: UInt64) async {
        while !typedLine.isEmpty {
            if Task.isCancelled { return }
            typedLine.removeLast()
            try? await Task.sleep(nanoseconds: charDelayMs * 1_000_000)
        }
    }

    private func pause(seconds: Double) async {
        let nanos = UInt64(seconds * 1_000_000_000)
        try? await Task.sleep(nanoseconds: nanos)
    }

    @MainActor
    private func startScanFlow() async {
        guard RoomCaptureSession.isSupported else {
            scanIssue = .unsupportedDevice
            return
        }

        let status = AVCaptureDevice.authorizationStatus(for: .video)
        switch status {
        case .authorized:
            scanManager.capturedPhotos.removeAll()
            isScanning = true
        case .notDetermined:
            let granted = await requestCameraAccess()
            if granted {
                scanManager.capturedPhotos.removeAll()
                isScanning = true
            } else {
                scanIssue = .cameraDenied
            }
        case .denied, .restricted:
            scanIssue = .cameraDenied
        @unknown default:
            scanIssue = .cameraDenied
        }
    }

    private func requestCameraAccess() async -> Bool {
        await withCheckedContinuation { continuation in
            AVCaptureDevice.requestAccess(for: .video) { granted in
                continuation.resume(returning: granted)
            }
        }
    }
}

private enum ScanIssue: Equatable {
    case unsupportedDevice
    case cameraDenied

    var title: String {
        switch self {
        case .unsupportedDevice: return "RoomPlan Not Supported"
        case .cameraDenied: return "Camera Permission Needed"
        }
    }

    var message: String {
        switch self {
        case .unsupportedDevice:
            return "This device does not support RoomPlan with LiDAR. Use a LiDAR-capable iPhone or iPad to scan."
        case .cameraDenied:
            return "Dots needs camera access to render the RoomPlan scan feed."
        }
    }
}

// MARK: - Shared Dots UI

enum DotsTheme {
    static let background = Color.black
    static let panel = Color.white.opacity(0.06)
    static let panelStrong = Color.white.opacity(0.09)
    static let border = Color.white.opacity(0.18)
    static let primaryText = Color.white
    static let secondaryText = Color.white.opacity(0.7)
    static let tertiaryText = Color.white.opacity(0.5)
}

struct DotsBrailleDIcon: View {
    let dotDiameter: CGFloat

    private let activeDots: Set<Int> = [1, 4, 5]

    var body: some View {
        HStack(spacing: dotDiameter * 0.5) {
            VStack(spacing: dotDiameter * 0.4) {
                brailleDot(1)
                brailleDot(2)
                brailleDot(3)
            }

            VStack(spacing: dotDiameter * 0.4) {
                brailleDot(4)
                brailleDot(5)
                brailleDot(6)
            }
        }
        .accessibilityHidden(true)
    }

    private func brailleDot(_ index: Int) -> some View {
        Circle()
            .fill(activeDots.contains(index) ? Color.white : Color.white.opacity(0.2))
            .frame(width: dotDiameter, height: dotDiameter)
    }
}

struct DotsWordmark: View {
    let textSize: CGFloat
    let dotDiameter: CGFloat
    let weight: Font.Weight

    var body: some View {
        HStack(spacing: max(6, dotDiameter * 0.85)) {
            DotsBrailleDIcon(dotDiameter: dotDiameter)
            Text("Dots.")
                .font(.system(size: textSize, weight: weight, design: .rounded))
                .foregroundStyle(DotsTheme.primaryText)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Dots")
    }
}

struct DotsPrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 19, weight: .semibold, design: .rounded))
            .foregroundStyle(Color.black.opacity(configuration.isPressed ? 0.75 : 0.95))
            .padding(.horizontal, 18)
            .padding(.vertical, 17)
            .background(
                RoundedRectangle(cornerRadius: 15)
                    .fill(Color.white.opacity(configuration.isPressed ? 0.85 : 0.96))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 15)
                    .stroke(Color.white.opacity(0.25), lineWidth: 1)
            )
    }
}

struct DotsSecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 18, weight: .medium, design: .rounded))
            .foregroundStyle(DotsTheme.primaryText.opacity(configuration.isPressed ? 0.82 : 0.96))
            .padding(.horizontal, 18)
            .padding(.vertical, 16)
            .background(
                RoundedRectangle(cornerRadius: 15)
                    .fill(DotsTheme.panelStrong.opacity(configuration.isPressed ? 0.65 : 1.0))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 15)
                    .stroke(DotsTheme.border.opacity(configuration.isPressed ? 0.4 : 1.0), lineWidth: 1)
            )
    }
}

struct DotsPanelModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(18)
            .background(
                RoundedRectangle(cornerRadius: 18)
                    .fill(DotsTheme.panel)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 18)
                    .stroke(DotsTheme.border, lineWidth: 1)
            )
    }
}

extension View {
    func dotsPanel() -> some View {
        modifier(DotsPanelModifier())
    }
}

// MARK: - Hex Color Extension

extension Color {
    init(hex: String) {
        let hex = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var int: UInt64 = 0
        Scanner(string: hex).scanHexInt64(&int)
        let a, r, g, b: UInt64
        switch hex.count {
        case 3:
            (a, r, g, b) = (255, (int >> 8) * 17, (int >> 4 & 0xF) * 17, (int & 0xF) * 17)
        case 6:
            (a, r, g, b) = (255, int >> 16, int >> 8 & 0xFF, int & 0xFF)
        case 8:
            (a, r, g, b) = (int >> 24, int >> 16 & 0xFF, int >> 8 & 0xFF, int & 0xFF)
        default:
            (a, r, g, b) = (255, 0, 0, 0)
        }
        self.init(
            .sRGB,
            red: Double(r) / 255,
            green: Double(g) / 255,
            blue: Double(b) / 255,
            opacity: Double(a) / 255
        )
    }
}

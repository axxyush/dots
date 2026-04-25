import SwiftUI
import RoomPlan

// MARK: - Coordinator (Top-Level Class)

final class RoomCaptureViewCoordinator: NSObject, RoomCaptureViewDelegate, NSCoding {
    let onComplete: (CapturedRoom) -> Void
    let onCancel: () -> Void

    init(onComplete: @escaping (CapturedRoom) -> Void, onCancel: @escaping () -> Void) {
        self.onComplete = onComplete
        self.onCancel = onCancel
    }

    required init(coder: NSCoder) {
        self.onComplete = { _ in }
        self.onCancel = { }
        super.init()
    }

    func encode(with coder: NSCoder) {
        // Closures cannot be encoded, so we're doing nothing here
    }

    // MARK: - RoomCaptureViewDelegate

    nonisolated func captureView(shouldPresent roomDataForProcessing: CapturedRoomData, error: (any Error)?) -> Bool {
        if error != nil {
            Task { @MainActor in self.onCancel() }
            return false
        }
        return true
    }

    nonisolated func captureView(didPresent processedResult: CapturedRoom, error: (any Error)?) {
        Task { @MainActor in
            if error == nil {
                self.onComplete(processedResult)
            } else {
                self.onCancel()
            }
        }
    }
}

// MARK: - UIViewRepresentable

struct RoomCaptureViewRepresentable: UIViewRepresentable {
    let onComplete: (CapturedRoom) -> Void
    let onCancel: () -> Void
    @Binding var stopRequested: Bool
    @ObservedObject var scanManager: ScanManager

    func makeCoordinator() -> RoomCaptureViewCoordinator {
        RoomCaptureViewCoordinator(onComplete: onComplete, onCancel: onCancel)
    }

    func makeUIView(context: Context) -> RoomCaptureView {
        let view = RoomCaptureView()
        view.delegate = context.coordinator
        let config = RoomCaptureSession.Configuration()
        view.captureSession.run(configuration: config)
        
        DispatchQueue.main.async {
            self.scanManager.attachToSession(view.captureSession)
        }
        
        return view
    }

    func updateUIView(_ uiView: RoomCaptureView, context: Context) {
        if stopRequested {
            uiView.captureSession.stop()
        }
    }

    static func dismantleUIView(_ uiView: RoomCaptureView, coordinator: RoomCaptureViewCoordinator) {
        uiView.captureSession.stop()
    }
}

import SwiftUI

struct TalkToAgentView: View {
    let roomId: String
    let onClose: () -> Void

    @StateObject private var session = ElevenLabsSession()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            VStack(spacing: 12) {
                topBar
                titleBlock
                transcriptArea
                Spacer(minLength: 10)
                VoiceWaveformView(isActive: session.state.isLive && !session.isMicPaused)
                    .frame(height: 44)
                controlBar
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 20)
        }
        .task {
            await session.start(roomId: roomId)
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background {
                session.end()
            }
        }
        .onDisappear {
            session.end()
        }
        .preferredColorScheme(.dark)
    }

    private var topBar: some View {
        HStack {
            Button {
                session.end()
                onClose()
            } label: {
                ZStack {
                    Circle()
                        .fill(Color.white.opacity(0.08))
                        .frame(width: 40, height: 40)
                    Image(systemName: "chevron.left")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.white)
                }
            }

            Spacer()

            Text("Voice Agent")
                .font(.headline.weight(.medium))
                .foregroundStyle(.white.opacity(0.8))

            Spacer()

            DotsWordmark(textSize: 25, dotDiameter: 5, weight: .semibold)
                .frame(width: 84, alignment: .trailing)
        }
    }

    private var titleBlock: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("CONSUMER VIEW · BUILDING")
                .font(.system(size: 14, weight: .medium, design: .monospaced))
                .foregroundStyle(Color.white.opacity(0.45))

            Text("Talking with a guest")
                .font(.system(size: 45, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)

            Text(statusLine)
                .font(.system(size: 19, weight: .regular, design: .monospaced))
                .foregroundStyle(Color.white.opacity(0.74))

            if case .error(let message) = session.state {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.red.opacity(0.9))
                    .lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 2)
    }

    private var transcriptArea: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if session.transcript.isEmpty {
                        Text("Agent transcript will appear here once the conversation starts.")
                            .font(.subheadline)
                            .foregroundStyle(Color.white.opacity(0.42))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 8)
                    }

                    ForEach(session.transcript) { line in
                        TranscriptBubble(line: line)
                            .id(line.id)
                    }
                }
                .padding(.vertical, 10)
            }
            .scrollIndicators(.hidden)
            .onChange(of: session.transcript.count) { _, _ in
                if let last = session.transcript.last {
                    withAnimation(.easeOut(duration: 0.2)) {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
        }
    }

    private var controlBar: some View {
        HStack(spacing: 18) {
            Button {
                session.toggleMicPause()
            } label: {
                Image(systemName: session.isMicPaused ? "play.fill" : "pause.fill")
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 58, height: 58)
                    .background(
                        Circle().fill(Color.white.opacity(0.07))
                    )
                    .overlay(
                        Circle().stroke(Color.white.opacity(0.15), lineWidth: 1)
                    )
            }
            .accessibilityLabel(session.isMicPaused ? "Resume listening" : "Pause listening")

            Button {
                session.toggleMicPause()
            } label: {
                Image(systemName: session.isMicPaused ? "mic.slash.fill" : "mic.fill")
                    .font(.system(size: 28, weight: .semibold))
                    .foregroundStyle(.black)
                    .frame(width: 88, height: 88)
                    .background(Circle().fill(Color.white))
                    .overlay(
                        Circle().stroke(Color.white.opacity(0.25), lineWidth: 1)
                    )
            }
            .accessibilityLabel(session.isMicPaused ? "Microphone paused" : "Microphone live")

            Button {
                session.end()
                onClose()
            } label: {
                Image(systemName: "phone.down.fill")
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 58, height: 58)
                    .background(
                        Circle().fill(Color.white.opacity(0.07))
                    )
                    .overlay(
                        Circle().stroke(Color.white.opacity(0.15), lineWidth: 1)
                    )
            }
            .accessibilityLabel("End conversation")
        }
    }

    private var statusLine: String {
        if session.isMicPaused {
            return "live · paused"
        }

        switch session.state {
        case .connecting, .fetchingToken, .requestingPermission:
            return "connecting..."
        case .speaking:
            return "live · speaking"
        case .listening:
            return "live · listening"
        case .ended:
            return "ended"
        case .error:
            return "error"
        case .idle:
            return "ready"
        }
    }
}

private struct VoiceWaveformView: View {
    let isActive: Bool

    private let bars: [CGFloat] = [8, 13, 9, 17, 15, 20, 26, 34, 42, 27, 21, 16, 10, 12, 22, 30, 37, 28, 18, 14, 10, 8]

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.18)) { timeline in
            let tick = Int(timeline.date.timeIntervalSinceReferenceDate * 10)

            HStack(spacing: 3.8) {
                ForEach(Array(bars.enumerated()), id: \.offset) { index, base in
                    let pulse = isActive ? abs(sin(Double(tick + index) * 0.33)) : 0.25
                    let height = max(6, base * CGFloat(0.35 + pulse * 0.75))
                    Capsule()
                        .fill(Color.white.opacity(isActive ? 0.9 : 0.4))
                        .frame(width: 4.3, height: min(44, height))
                }
            }
            .frame(maxWidth: .infinity)
        }
    }
}

private struct TranscriptBubble: View {
    let line: TranscriptLine

    var body: some View {
        HStack(alignment: .top) {
            if line.speaker == .user { Spacer(minLength: 46) }

            Text(line.text)
                .font(.system(size: 21, weight: .regular, design: .rounded))
                .foregroundStyle(line.speaker == .user ? .black : .white)
                .padding(.horizontal, 15)
                .padding(.vertical, 12)
                .background(
                    RoundedRectangle(cornerRadius: 16)
                        .fill(line.speaker == .user ? Color.white : Color.white.opacity(0.1))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(line.speaker == .user ? Color.white.opacity(0.25) : Color.white.opacity(0.12), lineWidth: 1)
                )

            if line.speaker == .agent { Spacer(minLength: 46) }
        }
    }
}

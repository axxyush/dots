import Foundation

#if canImport(ZeticMLange)
import ZeticMLange
#endif

/// Result from intent resolution — either a navigation destination or a Q&A answer.
enum IntentResult {
    /// User wants to navigate somewhere.
    case navigate(destinationName: String)
    /// User asked a question — reply with this text.
    case answer(text: String)
    /// Could not resolve anything.
    case unknown
}

/// On-device LLM intent router via ZETIC Melange.
/// Maps natural language like "I need to wash my hands" → "Bathroom",
/// and answers contextual questions like "How many doors are there?" using room data.
final class ZeticIntentRouter: ObservableObject, @unchecked Sendable {
    @Published private(set) var isModelLoaded = false

    #if canImport(ZeticMLange)
    private var model: ZeticMLangeLLMModel?
    #endif

    /// Loads the Gemma-4 LLM from Melange. Call once on app/session start.
    func loadModel() async {
        guard !isModelLoaded else { return }

        #if canImport(ZeticMLange)
        do {
            let loaded = try ZeticMLangeLLMModel(
                personalKey: AppSecrets.zeticPersonalKey,
                name: "changgeun/gemma-4-E2B-it",
                version: 1,
                modelMode: LLMModelMode.RUN_SPEED,
                onDownload: { progress in
                    print("[ZETIC LLM] Downloading model: \(Int(progress * 100))%")
                }
            )
            self.model = loaded
            self.isModelLoaded = true
            print("[ZETIC LLM] Gemma model loaded successfully.")
        } catch {
            print("[ZETIC LLM] Failed to load model: \(error.localizedDescription)")
        }
        #else
        print("[ZETIC LLM] ZeticMLange framework not available. Skipping model load.")
        #endif
    }

    // MARK: - Unified Intent Resolution

    /// Resolves user text into either a navigation command or a Q&A answer.
    func resolveIntent(
        userText: String,
        destinations: [String],
        roomContext: String = ""
    ) async -> IntentResult {
        // First check if it's a question about the room
        if isRoomQuestion(userText) {
            if let answer = await answerRoomQuestion(userText: userText, roomContext: roomContext) {
                return .answer(text: answer)
            }
            // Fallback: try to answer from room context offline
            if let offlineAnswer = offlineRoomAnswer(userText: userText, roomContext: roomContext) {
                return .answer(text: offlineAnswer)
            }
        }

        // Try LLM navigation intent
        if let llmResult = await runLLMIntent(userText: userText, destinations: destinations) {
            return .navigate(destinationName: llmResult)
        }

        // Keyword fallback for navigation
        if let keyword = keywordMatch(userText: userText, destinations: destinations) {
            return .navigate(destinationName: keyword)
        }

        return .unknown
    }

    /// Simple resolve that returns just the destination name (backward compat).
    func resolveDestination(userText: String, destinations: [String]) async -> String? {
        let result = await resolveIntent(userText: userText, destinations: destinations)
        if case .navigate(let name) = result { return name }
        return nil
    }

    // MARK: - Question Detection

    private func isRoomQuestion(_ text: String) -> Bool {
        let lowered = text.lowercased()
        let questionKeywords = [
            "how many", "what", "which", "where", "is there", "are there",
            "tell me about", "describe", "list", "count", "any", "do we have"
        ]
        return questionKeywords.contains { lowered.contains($0) }
    }

    // MARK: - Room Q&A via LLM

    private func answerRoomQuestion(userText: String, roomContext: String) async -> String? {
        guard !roomContext.isEmpty else { return nil }

        #if canImport(ZeticMLange)
        guard let model else { return nil }

        let prompt = """
        You are a helpful indoor navigation assistant for a visually impaired person.
        Here is the room layout information:
        \(roomContext)

        The user asks: "\(userText)"

        Give a brief, friendly answer in 1-2 sentences. Be specific with numbers and names.
        """

        do {
            try model.run(prompt)
            var buffer = ""
            while true {
                let waitResult = model.waitForNextToken()
                if waitResult.generatedTokens == 0 { break }
                buffer.append(waitResult.token)
                if buffer.count > 200 { break }
            }

            let cleaned = buffer.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !cleaned.isEmpty else { return nil }
            print("[ZETIC LLM] Q&A answer: \(cleaned)")
            return cleaned
        } catch {
            print("[ZETIC LLM] Q&A error: \(error.localizedDescription)")
            return nil
        }
        #else
        return nil
        #endif
    }

    // MARK: - Offline Room Answers (No LLM fallback)

    private func offlineRoomAnswer(userText: String, roomContext: String) -> String? {
        let lowered = userText.lowercased()
        let lines = roomContext.components(separatedBy: "\n")
        guard !lines.isEmpty else { return nil }

        // "How many objects/doors/windows?"
        if lowered.contains("how many") {
            if lowered.contains("door") {
                if let doorLine = lines.first(where: { $0.contains("Doors:") }),
                   let count = extractCount(from: doorLine, key: "Doors:") {
                    return "There are \(count) doors in this room."
                }
            }
            if lowered.contains("window") {
                if let windowLine = lines.first(where: { $0.contains("Windows:") }),
                   let count = extractCount(from: windowLine, key: "Windows:") {
                    return "There are \(count) windows in this room."
                }
            }
            if lowered.contains("object") || lowered.contains("thing") || lowered.contains("item") {
                if let objLine = lines.first(where: { $0.contains("Objects:") }),
                   let count = extractCount(from: objLine, key: "Objects:") {
                    return "There are \(count) objects in this room."
                }
            }
            if lowered.contains("wall") {
                if let wallLine = lines.first(where: { $0.contains("Walls:") }),
                   let count = extractCount(from: wallLine, key: "Walls:") {
                    return "There are \(count) walls in this room."
                }
            }
        }

        // "What objects are in the room?"
        if lowered.contains("what") && (lowered.contains("object") || lowered.contains("furniture") || lowered.contains("in the room")) {
            let objects = lines.filter { $0.hasPrefix("- Object:") }
                .compactMap { $0.components(separatedBy: ":").dropFirst().first?.trimmingCharacters(in: .whitespaces) }
                .map { $0.components(separatedBy: " (").first ?? $0 }
            if !objects.isEmpty {
                return "I can see: \(objects.joined(separator: ", "))."
            }
        }

        // "How big is the room?"
        if lowered.contains("dimension") || lowered.contains("big") || lowered.contains("size") {
            if let dimLine = lines.first(where: { $0.contains("Room dimensions:") }) {
                return dimLine
            }
        }

        return nil
    }

    private func extractCount(from line: String, key: String) -> Int? {
        guard let range = line.range(of: key) else { return nil }
        let afterKey = line[range.upperBound...].trimmingCharacters(in: .whitespaces)
        let numStr = afterKey.prefix(while: { $0.isNumber })
        return Int(numStr)
    }

    // MARK: - LLM Navigation Intent

    private func runLLMIntent(userText: String, destinations: [String]) async -> String? {
        #if canImport(ZeticMLange)
        guard let model else { return nil }

        let destinationList = destinations.joined(separator: ", ")
        let prompt = """
        You are a navigation assistant. Given these room locations: [\(destinationList)], \
        which one does the user want to go to? \
        User said: "\(userText)". \
        Reply with ONLY the exact location name from the list, nothing else.
        """

        do {
            try model.run(prompt)

            var buffer = ""
            while true {
                let waitResult = model.waitForNextToken()
                let token = waitResult.token
                let generatedTokens = waitResult.generatedTokens

                if generatedTokens == 0 {
                    break
                }
                buffer.append(token)

                // Safety: stop after 100 tokens (we only need a short name)
                if buffer.count > 100 { break }
            }

            let cleaned = buffer.trimmingCharacters(in: .whitespacesAndNewlines)
            print("[ZETIC LLM] Raw output: \(cleaned)")

            // Find the best matching destination from the LLM output
            return matchDestination(from: cleaned, candidates: destinations)
        } catch {
            print("[ZETIC LLM] Inference error: \(error.localizedDescription)")
            return nil
        }
        #else
        return nil
        #endif
    }

    // MARK: - Keyword Fallback

    /// Simple keyword matching: checks if any destination name appears in the user text,
    /// or if common synonyms map to known categories.
    private func keywordMatch(userText: String, destinations: [String]) -> String? {
        let lowered = userText.lowercased()

        // Direct name match
        for dest in destinations {
            if lowered.contains(dest.lowercased()) {
                return dest
            }
        }

        // Synonym mapping
        let synonyms: [(keywords: [String], categories: [String])] = [
            (["wash", "hands", "restroom", "toilet", "bathroom", "wc"], ["bathroom", "toilet", "restroom"]),
            (["sit", "couch", "sofa", "relax"], ["sofa", "couch", "chair"]),
            (["eat", "food", "dining", "table", "meal"], ["dining table", "table", "kitchen"]),
            (["sleep", "rest", "bed", "nap", "bedroom"], ["bed", "bedroom"]),
            (["cook", "kitchen", "stove", "fridge"], ["kitchen", "stove", "refrigerator"]),
            (["exit", "leave", "door", "entrance", "front"], ["door", "entrance", "exit"]),
            (["tv", "watch", "television", "screen"], ["tv", "television"]),
        ]

        for (keywords, categories) in synonyms {
            if keywords.contains(where: { lowered.contains($0) }) {
                for dest in destinations {
                    let destLower = dest.lowercased()
                    if categories.contains(where: { destLower.contains($0) }) {
                        return dest
                    }
                }
            }
        }

        return nil
    }

    /// Fuzzy-match LLM output against known destination names.
    private func matchDestination(from output: String, candidates: [String]) -> String? {
        let outputLower = output.lowercased()

        // Exact match first
        for candidate in candidates {
            if outputLower == candidate.lowercased() {
                return candidate
            }
        }

        // Substring match
        for candidate in candidates {
            if outputLower.contains(candidate.lowercased()) {
                return candidate
            }
        }

        // Reverse: check if candidate appears in the output
        for candidate in candidates {
            if candidate.lowercased().contains(outputLower) && !outputLower.isEmpty {
                return candidate
            }
        }

        return nil
    }
}

import Foundation

struct GeminiRoomAnswerService: Sendable {
    private let urlSession: URLSession = .shared

    func answer(question: String, roomSummary: String, roomJSON: String) async -> String? {
        let trimmedQuestion = question.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedJSON = roomJSON.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedQuestion.isEmpty, !trimmedJSON.isEmpty else { return nil }
        guard !AppSecrets.geminiApiKey.isEmpty else { return nil }

        guard let request = makeRequest(question: trimmedQuestion, roomSummary: roomSummary, roomJSON: trimmedJSON) else {
            return nil
        }

        do {
            let (data, response) = try await urlSession.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                print("[Gemini] Non-HTTP response.")
                return nil
            }

            guard (200...299).contains(http.statusCode) else {
                let body = String(data: data, encoding: .utf8) ?? "(empty)"
                print("[Gemini] Request failed: \(http.statusCode) \(body)")
                return nil
            }

            let decoded = try JSONDecoder().decode(GeminiGenerateContentResponse.self, from: data)
            let answer = decoded.candidates?
                .compactMap { candidate in
                    candidate.content?.parts?.compactMap(\.text).joined(separator: " ")
                }
                .first?
                .trimmingCharacters(in: .whitespacesAndNewlines)

            guard let answer, !answer.isEmpty else { return nil }
            return cleanedAnswer(answer)
        } catch {
            print("[Gemini] Room answer request failed: \(error.localizedDescription)")
            return nil
        }
    }

    private func makeRequest(question: String, roomSummary: String, roomJSON: String) -> URLRequest? {
        let model = AppSecrets.geminiModel.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !model.isEmpty else { return nil }

        guard let url = URL(string: "https://generativelanguage.googleapis.com/v1beta/models/\(model):generateContent") else {
            return nil
        }

        let prompt = """
        You are a concise indoor navigation assistant for a blind or low-vision user.

        Answer the user's question only from the saved room data below.
        If the answer is not present in the data, say that the saved room model does not contain enough information.
        Prefer saved labels such as Door 1, Door 2, and object labels when available.
        Keep the reply under 2 sentences and avoid markdown.

        Room summary:
        \(roomSummary)

        Saved room JSON:
        \(roomJSON)

        User question:
        \(question)
        """

        let requestBody = GeminiGenerateContentRequest(
            contents: [
                GeminiContent(
                    role: "user",
                    parts: [GeminiPart(text: prompt)]
                )
            ],
            generationConfig: GeminiGenerationConfig(
                temperature: 0.2,
                maxOutputTokens: 180
            )
        )

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 20
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(AppSecrets.geminiApiKey, forHTTPHeaderField: "x-goog-api-key")
        request.httpBody = try? JSONEncoder().encode(requestBody)
        return request
    }

    private func cleanedAnswer(_ answer: String) -> String {
        answer
            .replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "  ", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

private struct GeminiGenerateContentRequest: Encodable {
    let contents: [GeminiContent]
    let generationConfig: GeminiGenerationConfig?
}

private struct GeminiContent: Encodable {
    let role: String?
    let parts: [GeminiPart]
}

private struct GeminiPart: Codable {
    let text: String?
}

private struct GeminiGenerationConfig: Encodable {
    let temperature: Double?
    let maxOutputTokens: Int?
}

private struct GeminiGenerateContentResponse: Decodable {
    let candidates: [GeminiCandidate]?
}

private struct GeminiCandidate: Decodable {
    let content: GeminiCandidateContent?
}

private struct GeminiCandidateContent: Decodable {
    let parts: [GeminiPart]?
}

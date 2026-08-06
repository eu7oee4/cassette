import Foundation

/// 后端 /chat 的返回结构。字段名和后端 JSON 一致。
struct ChatResponse: Decodable {
    let reply: String
    let session_id: String
    let stored: [StoredMemory]?     // 这轮工具调用的结构化产物（长期记忆等，后续模块）
    let sticker_sends: [String]?    // 这轮他要发的表情（sticker id）
    let desc_updates: [DescUpdate]? // 这轮他改的表情描述
    let next_wake_hint: String?     // 这轮他若定了下次醒来 → 现成灰字提示文案；没定则 nil
}

/// 模型改了某张表情的描述。
struct DescUpdate: Decodable {
    let id: String
    let description: String
}

/// 一次记忆操作（tool: hold/grow/trace…，text: 内容摘要）。
struct StoredMemory: Decodable {
    let tool: String
    let text: String
}

/// 后端待送达盒子（/pending）里的一条：断连补投 / 主动消息 / 空产出错误标记。
struct PendingMessage: Decodable, Identifiable {
    let id: String
    let ts: Int
    let text: String
    let sticker_ids: [String]?   // 这条消息配的表情（按 id，app 取本地图上屏）；醒来发表情用
    let req_id: String?          // 断连补投的关联 id：撤半截气泡换完整回复用
    let error: Bool?             // true＝那轮没产出（claude 挂了）：清等待+提示重发，不撤半截
}

/// 发给后端的一条历史消息。
private struct OutMessage: Encodable {
    let role: String   // "user"（我） / "assistant"（对方）
    let text: String
    let ts: Int        // Unix 秒：后端用来算「现在几点 / 隔了多久」
}

/// 附给"最新这条"的图片（base64），后端带图走多模态让模型真看到。
private struct ImageOut: Encodable {
    let data: String          // base64
    let media_type: String
}

/// 发给后端的请求体：完整对话历史（最后一条是用户新消息）。
/// 后端无状态，靠这份历史理解上下文。session_id 仅供记账，nil 时自动省略。
private struct ChatRequestBody: Encodable {
    let messages: [OutMessage]
    let session_id: String?
    let stickers: [Sticker]?   // 表情库清单(id+描述)，供模型挑着发/改描述
    let client_req_id: String? // 断连补投的关联 id：后端 rescue 条目带回，app 用它替换半截气泡
    let images: [ImageOut]?    // 最新这条附带的图片；nil 自动省略
}

/// 面向用户的错误类型，errorDescription 直接拿去给用户看。
enum ChatServiceError: LocalizedError {
    case badURL
    case cannotConnect          // 服务器没开 / 连不上（从没建立过连接）
    case connectionLost         // 连上后中途断（典型：生成中切后台被系统掐）——断连补投会兜，别当错误吓人
    case timedOut
    case server(String)         // 后端返回的非 2xx（含 detail 文案）
    case badResponse            // 响应无法解析

    var errorDescription: String? {
        switch self {
        case .badURL:         return "后端地址配置有误。"
        case .cannotConnect:  return "连不上后端，检查一下服务器开没开、地址对不对。"
        case .connectionLost: return "连接中途断了，回复稍后会自动补回来。"
        case .timedOut:       return "请求超时了，服务器可能在忙或没响应。"
        case .server(let m): return "后端返回错误：\(m)"
        case .badResponse:   return "收到的数据看不懂（格式不对）。"
        }
    }
}

/// /chat/stream 的一条 SSE 事件（后端统一协议）。
enum StreamEvent {
    case text(String)                        // 正文片段，追加进当前流式气泡
    case textBreak                           // 当前气泡定稿保留，下一段正文另起新气泡（工具调用切段）
    case memory(tool: String, text: String)  // 这轮记忆操作 → 内联灰字
    case error(String)                       // 出错提示
    case done(ChatResponse?)                 // 结束：附完整 ChatResponse（错误/空回复时为 nil）
}

/// 负责和后端通信。用 async/await。
struct ChatService {
    /// 每次发给后端的对话历史上限：只发最近 N 条，防止 prompt 随聊天无限膨胀顶爆上下文。
    /// 完整历史仍全存在本地（翻看/搜索用）。UserDefaults 持久化，没设过=默认 100，夹取 20~1000。
    static let defaultHistoryCap = 100
    static var sendHistoryCap: Int {
        let v = UserDefaults.standard.integer(forKey: "sendHistoryCap")
        return v == 0 ? defaultHistoryCap : min(max(v, 20), 1000)
    }

    /// 构造发给后端 /chat 或 /chat/stream 的请求。
    /// history 应以用户的新消息结尾。memoryNote 是纯 UI 灰字、不发回后端；历史裁到最近 sendHistoryCap 条。
    private func buildChatRequest(path: String, history: [ChatMessage], sessionId: String?,
                                  stickers: [Sticker] = [], reqId: String? = nil,
                                  imagesData: [Data] = []) throws -> URLRequest {
        guard let url = URL(string: BackendConfig.baseURL + path) else {
            throw ChatServiceError.badURL
        }
        let outMessages = history.filter { !$0.isMemoryNote }
            .suffix(Self.sendHistoryCap)
            .map {
                OutMessage(role: $0.sender == .me ? "user" : "assistant",
                           text: $0.plainText,
                           ts: Int($0.timestamp.timeIntervalSince1970))
            }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(BackendConfig.authKey, forHTTPHeaderField: "X-Auth")
        request.timeoutInterval = 600   // 空闲计时（收到数据就重置）：流式有后端心跳撑着，
                                        // 这里只兜"后端整个没响应"
        let images: [ImageOut]? = imagesData.isEmpty ? nil :
            imagesData.map { ImageOut(data: $0.base64EncodedString(), media_type: "image/jpeg") }
        request.httpBody = try JSONEncoder().encode(
            ChatRequestBody(messages: outMessages, session_id: sessionId,
                            stickers: stickers.isEmpty ? nil : stickers,
                            client_req_id: reqId, images: images)
        )
        return request
    }

    /// 把常见网络错误翻译成友好提示。
    private func mapURLError(_ error: URLError) -> ChatServiceError {
        switch error.code {
        case .networkConnectionLost:
            // 连接建立后中途断：典型是生成中切后台/锁屏被系统掐流。后端照跑，
            // 断连守护会把完整回复补投 pending，app 回前台轮询补上——和"连不上"要区分开。
            return .connectionLost
        case .cannotConnectToHost, .cannotFindHost, .notConnectedToInternet:
            return .cannotConnect
        case .timedOut:
            return .timedOut
        default:
            return .cannotConnect
        }
    }

    /// 发送对话历史（非流式），返回后端生成的完整回复。保留做流式的降级。
    func send(history: [ChatMessage], sessionId: String?,
              stickers: [Sticker] = [], imagesData: [Data] = []) async throws -> ChatResponse {
        let request = try buildChatRequest(path: "/chat", history: history, sessionId: sessionId,
                                           stickers: stickers, imagesData: imagesData)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch let error as URLError {
            throw mapURLError(error)
        }
        guard let http = response as? HTTPURLResponse else {
            throw ChatServiceError.badResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONDecoder().decode([String: String].self, from: data))?["detail"]
            throw ChatServiceError.server(detail ?? "HTTP \(http.statusCode)")
        }
        do {
            return try JSONDecoder().decode(ChatResponse.self, from: data)
        } catch {
            throw ChatServiceError.badResponse
        }
    }

    /// 流式发送：返回一串 SSE 事件（text / text_break / memory / error / done）。
    /// 用 URLSession.bytes 逐行读；.lines 自带跨包缓冲，半截行不会炸。
    func sendStream(history: [ChatMessage], sessionId: String?,
                    stickers: [Sticker] = [], reqId: String? = nil,
                    imagesData: [Data] = []) -> AsyncThrowingStream<StreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let request = try buildChatRequest(path: "/chat/stream", history: history,
                                                       sessionId: sessionId, stickers: stickers,
                                                       reqId: reqId, imagesData: imagesData)
                    let (bytes, response) = try await URLSession.shared.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw ChatServiceError.badResponse
                    }
                    guard (200..<300).contains(http.statusCode) else {
                        // 流开始前的错误（4xx/5xx）：后端返普通 JSON {"detail":..}，读出来展示。
                        var body = Data()
                        for try await b in bytes { body.append(b) }
                        let detail = (try? JSONDecoder().decode([String: String].self, from: body))?["detail"]
                        throw ChatServiceError.server(detail ?? "HTTP \(http.statusCode)")
                    }
                    for try await line in bytes.lines {
                        guard line.hasPrefix("data: ") else { continue }
                        if let ev = Self.parseStreamEvent(String(line.dropFirst(6))) {
                            continuation.yield(ev)
                        }
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch let error as URLError {
                    continuation.finish(throwing: mapURLError(error))
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// 解析一条 SSE data 行（已去掉 "data: " 前缀）成 StreamEvent。
    /// 坏片段/不认识的类型（如心跳 ping）返回 nil、跳过。
    private static func parseStreamEvent(_ payload: String) -> StreamEvent? {
        guard let data = payload.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = obj["type"] as? String else { return nil }
        switch type {
        case "text":       return .text(obj["content"] as? String ?? "")
        case "text_break": return .textBreak
        case "memory":     return .memory(tool: obj["tool"] as? String ?? "",
                                          text: obj["text"] as? String ?? "")
        case "error":      return .error(obj["content"] as? String ?? "出错了")
        case "done":       return .done(try? JSONDecoder().decode(ChatResponse.self, from: data))
        default:           return nil
        }
    }

    // MARK: - 表情描述

    /// 传一张表情图，让模型写一句描述（画面+情绪），供以后挑选。
    func describeSticker(imageData: Data) async throws -> String {
        struct ImageOut: Encodable { let data: String; let media_type: String }
        struct Body: Encodable { let image: ImageOut }
        let body = try JSONEncoder().encode(
            Body(image: ImageOut(data: imageData.base64EncodedString(), media_type: "image/png"))
        )
        let data = try await perform(authedRequest("POST", "/describe_sticker", jsonBody: body,
                                                   timeout: 120))
        struct Resp: Decodable { let description: String }
        do { return try JSONDecoder().decode(Resp.self, from: data).description }
        catch { throw ChatServiceError.badResponse }
    }

    // MARK: - 主动消息设置

    /// 拉后端当前的主动消息设置。
    func getSettings() async throws -> ProactiveSettings {
        let data = try await perform(authedRequest("GET", "/settings"))
        do { return try JSONDecoder().decode(ProactiveSettings.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    /// 覆盖保存主动消息设置，返回后端确认后的值。
    @discardableResult
    func saveSettings(_ s: ProactiveSettings) async throws -> ProactiveSettings {
        let body = try JSONEncoder().encode(s)
        let data = try await perform(authedRequest("POST", "/settings", jsonBody: body))
        do { return try JSONDecoder().decode(ProactiveSettings.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    // MARK: - 待送达盒子（断连补投 / 主动消息）

    /// 拉取后端积压的、还没同步进本地的消息。
    func getPending() async throws -> [PendingMessage] {
        let data = try await perform(authedRequest("GET", "/pending"))
        struct Wrap: Decodable { let items: [PendingMessage] }
        do { return try JSONDecoder().decode(Wrap.self, from: data).items }
        catch { throw ChatServiceError.badResponse }
    }

    /// 告诉后端这些消息已经收进本地了（先存后 ack，防丢）。
    func ackPending(ids: [String]) async throws {
        let body = try JSONEncoder().encode(["ids": ids])
        _ = try await perform(authedRequest("POST", "/pending/ack", jsonBody: body))
    }

    /// 后端正在跑的聊天轮（client_req_id 集合）。断流后对账用：不在跑=这轮丢了。
    func chatActive() async throws -> [String] {
        let data = try await perform(authedRequest("GET", "/chat/active"))
        struct Wrap: Decodable { let active: [String] }
        do { return try JSONDecoder().decode(Wrap.self, from: data).active }
        catch { throw ChatServiceError.badResponse }
    }

    // MARK: - 内部请求工具

    // internal：MemoryPage 等功能页的服务扩展也走这两个（统一鉴权/错误翻译，别另起一套）
    func authedRequest(_ method: String, _ path: String, jsonBody: Data? = nil,
                       timeout: TimeInterval = 20) throws -> URLRequest {
        guard let url = URL(string: BackendConfig.baseURL + path) else { throw ChatServiceError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue(BackendConfig.authKey, forHTTPHeaderField: "X-Auth")
        req.timeoutInterval = timeout
        if let jsonBody {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = jsonBody
        }
        return req
    }

    /// 发请求 + 统一错误翻译 + 非 2xx 抛后端 detail。
    func perform(_ request: URLRequest) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch let error as URLError {
            switch error.code {
            case .timedOut: throw ChatServiceError.timedOut
            default:        throw ChatServiceError.cannotConnect
            }
        }
        guard let http = response as? HTTPURLResponse else { throw ChatServiceError.badResponse }
        guard (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONDecoder().decode([String: String].self, from: data))?["detail"]
            throw ChatServiceError.server(detail ?? "HTTP \(http.statusCode)")
        }
        return data
    }
}

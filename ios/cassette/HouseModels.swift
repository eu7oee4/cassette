import Foundation

/// 同居世界（大房子）的数据模型 + 请求（PLAN_cohabit C3）。
/// 字段名与后端 JSON 原样对齐（world.py / app.py 的 /world /rooms 路由），不做改名映射。

/// /world：房子视图的数据源。
struct WorldSnapshot: Decodable {
    let rooms: [WorldRoom]
    let entities: [String: WorldEntity]

    /// 在某个位置的实体 id（房间 id / "hallway" / "away"），顺序稳定（user 在前）。
    func ids(at location: String) -> [String] {
        entities.filter { $0.value.location == location }.map(\.key)
            .sorted { a, _ in a == "user" }
    }
}

struct WorldRoom: Decodable, Identifiable {
    let id: String
    let name: String
    let floor: Int
    let type: String          // bedroom | common | functional
    let owner: String?
    let lock: Int
    let state_count: Int
    let occupants: [String]
}

struct WorldEntity: Decodable {
    let location: String
    let name: String
    let status: String?       // "code" / "game" / nil（正在电脑前）

    var statusLabel: String? {
        switch status {
        case "code": return "正在敲代码"
        case "game": return "正在玩游戏"
        default:     return nil
        }
    }
}

/// /rooms/{id}：房间视图副栏（含地点状态全文）。
struct RoomDetail: Decodable {
    let id: String
    let name: String
    let floor: Int
    let lock: Int
    let owner: String?
    let state: [RoomStateEntry]
    let occupants: [String]
    let replying: [String]?   // 正在生成醒来回应的在场角色（「正在回应…」动画）
    let paused: Bool?         // 醒来队列暂停中（用户按住场面好插嘴）
}

struct RoomStateEntry: Decodable, Identifiable, Equatable {
    let id: String
    let text: String
    let author: String
    let since: Int
}

/// 房间事件流的一条（system / action / speech）。
struct RoomEvent: Decodable, Identifiable, Equatable {
    let id: String
    let ts: Int
    let type: String
    let actor: String
    let text: String
    let kind: String?         // system 事件才有：enter / leave / state
}

/// /world/move 的结果。门锁着不是错误：ok=false + text（「门锁着，没进去」）。
struct MoveResult: Decodable {
    let ok: Bool
    let from: String
    let to: String
    let reason: String?
    let text: String?
    let noop: Bool?
}

// MARK: - 请求（走 ChatService 的统一鉴权/错误翻译，别另起一套）

extension ChatService {
    func getWorld() async throws -> WorldSnapshot {
        let data = try await perform(authedRequest("GET", "/world", timeout: 8))
        return try JSONDecoder().decode(WorldSnapshot.self, from: data)
    }

    func worldMove(to: String) async throws -> MoveResult {
        let body = try JSONEncoder().encode(["to": to])
        let data = try await perform(authedRequest("POST", "/world/move", jsonBody: body, timeout: 8))
        return try JSONDecoder().decode(MoveResult.self, from: data)
    }

    func roomDetail(_ id: String) async throws -> RoomDetail {
        let data = try await perform(authedRequest("GET", "/rooms/\(id)", timeout: 8))
        return try JSONDecoder().decode(RoomDetail.self, from: data)
    }

    /// scope: "visible"（本次在场区间，房间视图）/ "all"（偷看的上帝视角）。
    func roomEvents(_ id: String, scope: String, limit: Int = 200) async throws -> [RoomEvent] {
        let data = try await perform(
            authedRequest("GET", "/rooms/\(id)/events?scope=\(scope)&limit=\(limit)", timeout: 8))
        struct Box: Decodable { let events: [RoomEvent] }
        return try JSONDecoder().decode(Box.self, from: data).events
    }

    /// 混写表达：*星号* 是动作、其余是说话，可交错；服务端拆成事件序列。
    func roomAct(_ id: String, text: String) async throws {
        let body = try JSONEncoder().encode(["text": text])
        _ = try await perform(authedRequest("POST", "/rooms/\(id)/act", jsonBody: body, timeout: 10))
    }

    /// 导演口：手写一段环境刺激（如「浴室传来水声」），点名让谁事件醒。
    /// 只入队醒因，不落房间事件——别的在场者不会「看见」这段旁白。
    func worldNudge(text: String, targets: [String]) async throws {
        struct Body: Encodable { let text: String; let targets: [String] }
        let body = try JSONEncoder().encode(Body(text: text, targets: targets))
        _ = try await perform(authedRequest("POST", "/world/nudge", jsonBody: body, timeout: 8))
    }

    /// 暂停/恢复醒来队列（正在生成的说完为止；恢复立即冲队）。
    func worldPause(_ on: Bool) async throws {
        let body = try JSONEncoder().encode(["on": on])
        _ = try await perform(authedRequest("POST", "/world/pause", jsonBody: body, timeout: 8))
    }

    func roomStateChange(_ id: String, op: String, entryID: String?, text: String?) async throws {
        struct Body: Encodable { let op: String; let id: String?; let text: String? }
        let body = try JSONEncoder().encode(Body(op: op, id: entryID, text: text))
        _ = try await perform(authedRequest("POST", "/rooms/\(id)/state", jsonBody: body, timeout: 10))
    }
}

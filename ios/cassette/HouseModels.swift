import Foundation

/// 同居世界（大房子）的数据模型 + 请求（PLAN_cohabit C3）。
/// 字段名与后端 JSON 原样对齐（world.py / app.py 的 /world /rooms 路由），不做改名映射。

/// /world：房子视图的数据源。
struct WorldSnapshot: Decodable {
    let rooms: [WorldRoom]
    let entities: [String: WorldEntity]
    let carry_offer: CarryOffer?      // 有人想抱你去别的房间（答应了才会一起移动）
    let paused: Bool?                 // 醒来队列暂停中
    let queue_error: String?          // 队列被按停的原因（模型过载）；按「开始」即清

    /// 在某个位置的实体 id（房间 id / "away"），顺序稳定（user 在前）。
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
    let queue_error: String?  // 队列被按停的原因（模型过载）；按「开始」即清
    let carry_offer: CarryOffer?   // 这个房间里挂着的抱人邀约（只在发生的房间带回）
    let pets: [String]?       // 在场的宠物（照顾入口的开关，PLAN_pet P3）
}

/// /pets/{id}：宠物状态（照顾面板数据源）。
struct PetInfo: Decodable {
    let id: String
    let name: String
    let location: String
    let state: PetStats
    let needs: [PetNeed]
}

struct PetStats: Decodable {
    let satiety: Int
    let hydration: Int
    let energy: Int
    let mood: Int
    let litter: Int           // 1 干净 / 2 有屎 / 3 满了
    let asleep: Bool
    let day: Int              // 养到第几天

    var litterLabel: String {
        switch litter {
        case 1: return "干净"
        case 2: return "有屎"
        default: return "满了"
        }
    }
}

struct PetNeed: Decodable, Identifiable {
    let kind: String
    let text: String
    var id: String { kind }
}

/// /pets/{id}/interact 的返回（新状态面板会重新拉，这里只要反应本身）。
struct PetReaction: Decodable {
    let reply: String
    let action: String
}

/// 抱人邀约：AI 醒来轮写了 MOVE+CARRY → 不立刻移动，先问你。
/// 答应 → 这时才 move+carry；拒绝/超时（约 2 分钟）→ 发起人收各自的补醒。
struct CarryOffer: Decodable, Equatable {
    let id: String
    let actor: String
    let actor_name: String
    let room: String          // 邀约发生（= 你俩此刻所在）的房间
    let to: String
    let to_name: String
    let deadline: Int
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
    let turn: String?         // 同一轮（一次醒来 / 一次发送）落下的事件共用；老历史没有
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

    /// 删掉一条记录**所在的那一轮**（左滑删除）：服务端按 turn 收整组，房间事件流 +
    /// 各角色的经历流同删，进出场那条留着（它是可见区间的骨架，删了 AI 反而看见更早的
    /// 历史）。删完下次注入立刻就变了。返回真删掉的条数。
    @discardableResult
    func roomDeleteTurn(_ id: String, eventID: String) async throws -> Int {
        let body = try JSONEncoder().encode(["id": eventID])
        let data = try await perform(authedRequest("POST", "/rooms/\(id)/events/delete",
                                                    jsonBody: body, timeout: 10))
        struct Box: Decodable { let deleted: Int }
        return (try? JSONDecoder().decode(Box.self, from: data).deleted) ?? 0
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

    /// 应答抱人邀约：答应 → 这时才真的一起移动；拒绝/已失效都不动。
    /// 邀约已经不在（过期/位置变了）→ 服务端 409，错误文案直接给用户看。
    func respondCarryOffer(id: String, accept: Bool) async throws {
        struct Body: Encodable { let id: String; let accept: Bool }
        let body = try JSONEncoder().encode(Body(id: id, accept: accept))
        _ = try await perform(authedRequest("POST", "/world/carry_offer",
                                            jsonBody: body, timeout: 10))
    }

    func roomStateChange(_ id: String, op: String, entryID: String?, text: String?) async throws {
        struct Body: Encodable { let op: String; let id: String?; let text: String? }
        let body = try JSONEncoder().encode(Body(op: op, id: entryID, text: text))
        _ = try await perform(authedRequest("POST", "/rooms/\(id)/state", jsonBody: body, timeout: 10))
    }

    // MARK: 宠物照料（PLAN_pet P3；用户与 pet MCP 共用同一套端点）

    func petInfo(_ id: String) async throws -> PetInfo {
        let data = try await perform(authedRequest("GET", "/pets/\(id)", timeout: 8))
        return try JSONDecoder().decode(PetInfo.self, from: data)
    }

    /// 投喂（罐罐/猫条/冻干）或自由互动白描。猫必醒，反应直接在返回里
    /// （动作/喵声也会落进房间事件流，轮询自然上屏）。要等猫引擎，超时给足。
    func petInteract(_ id: String, feed: String? = nil, text: String? = nil) async throws -> PetReaction {
        struct Body: Encodable { let actor: String; let feed: String?; let text: String? }
        let body = try JSONEncoder().encode(Body(actor: "user", feed: feed, text: text))
        let data = try await perform(authedRequest("POST", "/pets/\(id)/interact",
                                                   jsonBody: body, timeout: 90))
        return try JSONDecoder().decode(PetReaction.self, from: data)
    }

    /// 铲屎：人得在猫房（服务端校验，不在会 409）。
    func petScoop(_ id: String) async throws {
        let body = try JSONEncoder().encode(["actor": "user"])
        _ = try await perform(authedRequest("POST", "/pets/\(id)/scoop", jsonBody: body, timeout: 10))
    }
}

import SwiftUI

/// fullScreenCover(item:) 需要 Identifiable 的包装。urls 支持多图（堆叠卡点开翻页），单图就传一个。
struct EnlargedImage: Identifiable {
    let id = UUID()
    let urls: [URL]
    let start: Int

    init(urls: [URL], start: Int = 0) {
        self.urls = urls
        self.start = start
    }

    init(url: URL) {
        self.init(urls: [url], start: 0)
    }
}

/// 全屏图片查看器：黑底、左右翻页（多图）、双指缩放、单击或点右上角关闭。
/// onDeleteURL 非 nil 时长按当前图可删（确认弹窗）：回调交给持有方删消息，
/// 本地列表同步移除接着看剩下的，删空自动关闭。Search 的九宫格查看器不传=没有删除。
struct ImageViewerView: View {
    let onClose: () -> Void
    private let onDeleteURL: ((URL) -> Void)?
    @State private var urls: [URL]
    @State private var index: Int
    @State private var confirmDelete = false

    init(urls: [URL], start: Int = 0, onDeleteURL: ((URL) -> Void)? = nil,
         onClose: @escaping () -> Void) {
        self.onClose = onClose
        self.onDeleteURL = onDeleteURL
        _urls = State(initialValue: urls)
        _index = State(initialValue: min(max(start, 0), max(urls.count - 1, 0)))
    }

    /// 旧调用兼容：单图。
    init(url: URL, onClose: @escaping () -> Void) {
        self.init(urls: [url], start: 0, onClose: onClose)
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            TabView(selection: $index) {
                ForEach(Array(urls.enumerated()), id: \.offset) { i, url in
                    ZoomableImage(url: url, onTap: onClose,
                                  onLongPress: onDeleteURL == nil ? nil : { confirmDelete = true })
                        .tag(i)
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .never))

            VStack {
                HStack {
                    if urls.count > 1 {
                        Text("\(index + 1) / \(urls.count)")
                            .font(.footnote.bold())
                            .foregroundStyle(.white.opacity(0.85))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(Capsule().fill(.black.opacity(0.45)))
                            .padding()
                    }
                    Spacer()
                    Button(action: onClose) {
                        Image(systemName: "xmark.circle.fill")
                            .font(.title)
                            .foregroundStyle(.white.opacity(0.85))
                    }
                    .padding()
                }
                Spacer()
            }
        }
        .statusBarHidden()
        .confirmationDialog("删除这张照片？", isPresented: $confirmDelete,
                            titleVisibility: .visible) {
            Button("删除", role: .destructive) { deleteCurrent() }
        }
    }

    /// 删当前这张：先回调持有方删消息，再动本地列表（删空关查看器，删尾巴索引回退）。
    private func deleteCurrent() {
        guard urls.indices.contains(index) else { return }
        let url = urls[index]
        onDeleteURL?(url)
        urls.remove(at: index)
        if urls.isEmpty {
            onClose()
        } else if index >= urls.count {
            index = urls.count - 1
        }
    }
}

/// 单页：双指缩放 + 双击切换 1x/2x + 放大后单指拖拽平移 + 单击关闭 + 可选长按（删除入口）。
/// 拖拽只在放大时挂（highPriority 抢过 TabView 翻页）；缩回 1x 自动归位。
private struct ZoomableImage: View {
    let url: URL
    let onTap: () -> Void
    var onLongPress: (() -> Void)? = nil

    @State private var scale: CGFloat = 1
    @GestureState private var pinch: CGFloat = 1
    @State private var offset: CGSize = .zero
    @GestureState private var panDelta: CGSize = .zero

    private var panGesture: some Gesture {
        DragGesture(minimumDistance: 5)
            .updating($panDelta) { v, state, _ in state = v.translation }
            .onEnded { v in
                offset.width += v.translation.width
                offset.height += v.translation.height
            }
    }

    var body: some View {
        if let img = AppFiles.loadImage(url) {
            Image(uiImage: img)
                .resizable()
                .scaledToFit()
                .scaleEffect(scale * pinch)
                .offset(x: offset.width + panDelta.width,
                        y: offset.height + panDelta.height)
                .gesture(
                    MagnificationGesture()
                        .updating($pinch) { value, state, _ in state = value }
                        .onEnded { value in
                            scale = min(max(scale * value, 1), 5)   // 限制 1x~5x
                            if scale <= 1 { withAnimation { offset = .zero } }
                        }
                )
                // 放大才拖拽（mask 关掉时完全不参与）；1x 让给 TabView 翻页
                .highPriorityGesture(panGesture, including: scale > 1 ? .gesture : .none)
                .onTapGesture(count: 2) {                            // 双击切换，缩回时归位
                    withAnimation {
                        scale = scale > 1 ? 1 : 2
                        if scale <= 1 { offset = .zero }
                    }
                }
                .onTapGesture { onTap() }                                               // 单击关闭
                .onLongPressGesture { onLongPress?() }                                  // 长按删除（没回调=无操作）
        } else {
            Image(systemName: "photo")
                .font(.system(size: 48))
                .foregroundStyle(.white.opacity(0.6))
                .onTapGesture { onTap() }
        }
    }
}

import SwiftUI
import PhotosUI

/// 底部弹出的表情包面板：一行 5 格、可上下滚动。
/// 第一格是「从相册添加」，其余是已有表情包缩略图。
struct StickerPanel: View {
    @ObservedObject var store: StickerStore
    let onPick: (Sticker) -> Void          // 点某张表情包 → 作为图片消息发出

    @State private var pickerItem: PhotosPickerItem?
    @State private var editingSticker: Sticker?   // 长按某张 → 编辑描述/删除

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 8), count: 5)

    var body: some View {
        ScrollView {
            LazyVGrid(columns: columns, spacing: 8) {
                // 第一格：虚线框 + ➕，点它从相册选图加入表情包库。
                PhotosPicker(selection: $pickerItem, matching: .images) {
                    AddStickerCell()
                }
                .buttonStyle(.plain)

                // 其余格子：已有表情包缩略图。点＝发出；长按＝编辑描述/删除。
                ForEach(store.stickers) { sticker in
                    StickerThumbnail(url: store.imageURL(for: sticker),
                                     generating: store.describingIds.contains(sticker.id))
                        .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .onTapGesture { onPick(sticker) }
                        .onLongPressGesture { editingSticker = sticker }
                }
            }
            .padding(12)
        }
        .background(Color(.systemBackground))
        .onChange(of: pickerItem) { _, newItem in
            guard let newItem else { return }
            Task {
                if let data = try? await newItem.loadTransferable(type: Data.self) {
                    store.addSticker(from: data)
                }
                pickerItem = nil   // 重置，方便下次再选
            }
        }
        .sheet(item: $editingSticker) { sticker in
            StickerEditSheet(store: store, sticker: sticker)
        }
    }
}

/// 长按表情弹出的编辑页：看大图 + 改描述 + 删除。
private struct StickerEditSheet: View {
    @ObservedObject var store: StickerStore
    let sticker: Sticker
    @Environment(\.dismiss) private var dismiss
    @State private var draft: String = ""

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                if let img = UIImage(contentsOfFile: store.imageURL(for: sticker).path) {
                    Image(uiImage: img)
                        .resizable()
                        .scaledToFit()
                        .frame(maxHeight: 180)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
                TextField("Description", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(2...6)
                Button(role: .destructive) {
                    store.remove(id: sticker.id)
                    dismiss()
                } label: {
                    Label("Delete", systemImage: "trash")
                }
                .padding(.top, 4)
                Spacer()
            }
            .padding()
            .navigationTitle("Sticker")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button {
                        store.updateDescription(id: sticker.id,
                                                draft.trimmingCharacters(in: .whitespacesAndNewlines))
                        dismiss()
                    } label: { Image(systemName: "checkmark") }
                }
            }
            .onAppear { draft = sticker.description }
        }
    }
}

/// 「添加」格子：虚线圆角框，中间一个 ➕。
private struct AddStickerCell: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 12, style: .continuous)
            .strokeBorder(
                style: StrokeStyle(lineWidth: 1.5, dash: [5, 4])
            )
            .foregroundStyle(Color(.systemGray2))
            .aspectRatio(1, contentMode: .fit)
            .overlay(
                Image(systemName: "plus")
                    .font(.system(size: 22, weight: .medium))
                    .foregroundStyle(.tint)
            )
    }
}

/// 表情包缩略图：正方形、填充裁剪、圆角。生成描述时盖一层“生成中”。
private struct StickerThumbnail: View {
    let url: URL
    var generating: Bool = false

    var body: some View {
        Color(.systemGray6)
            .aspectRatio(1, contentMode: .fit)
            .overlay {
                if let image = UIImage(contentsOfFile: url.path) {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFill()
                } else {
                    Image(systemName: "photo")
                        .foregroundStyle(.secondary)
                }
            }
            .overlay {
                if generating {
                    ZStack {
                        Color.black.opacity(0.35)
                        ProgressView().tint(.white)
                    }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

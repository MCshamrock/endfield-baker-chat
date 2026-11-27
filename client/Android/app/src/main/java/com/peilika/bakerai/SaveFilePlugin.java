package com.peilika.bakerai;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.DocumentsContract;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.util.Base64;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

/**
 * SaveFile —— 安卓端的「导出文件」落地通道（前端 utils/nativeExport.ts 的对端）。
 *
 * 以前前端一整套 beginWrite / appendChunk / finishWrite / saveFile 都写好了，
 * 但原生侧从来没人实现过：registerPlugin('SaveFile') 拿到的是 Capacitor 的万能代理，
 * 真调下去只会 reject「plugin is not implemented」，用户看到的就是「点了导出没反应」。
 * 这个类把那条链补齐，并且做到：
 *
 *   1. 文件写到**公共下载目录**「下载/Baker AI/」，任何文件管理器都看得见
 *      （API 29+ 走 MediaStore，免存储权限；API 24~28 走应用外部目录，同样免权限）。
 *   2. 分块写入 + 逐块 SHA-256 回读校验，写完核对总长，绝不留半截文件。
 *   3. 保存成功后弹一个 Toast 报出落点，并提供 revealFile 直接跳文件管理器。
 */
@CapacitorPlugin(name = "SaveFile")
public class SaveFilePlugin extends Plugin {

    /** 公共下载目录下的子目录名（中文，用户一眼能找到） */
    private static final String SUB_DIR = "Baker AI";

    /** 分块写入的临时文件（写完整了才搬到公共目录，失败就删掉） */
    private static final class Pending {
        final File tmp;
        final String fileName;
        final String mime;
        long bytes;

        Pending(File tmp, String fileName, String mime) {
            this.tmp = tmp;
            this.fileName = fileName;
            this.mime = mime;
        }
    }

    /** 一次成功落盘的结果 */
    private static final class Saved {
        final String path;      // 给人看的落点：「下载/Baker AI/x.zip」
        final String uri;       // content:// 或 file://
        final boolean isPublic; // 是否落在公共下载目录
        final String location;  // public / external / internal

        Saved(String path, String uri, boolean isPublic, String location) {
            this.path = path;
            this.uri = uri;
            this.isPublic = isPublic;
            this.location = location;
        }
    }

    private final Map<String, Pending> pending = new HashMap<>();

    // ---------------------------------------------------------------- 自检

    /** 前端 probePlugin() 用它判断能不能走分块导出（chunked=true 才会走大文件通道） */
    @PluginMethod
    public void pluginInfo(PluginCall call) {
        JSObject out = new JSObject();
        out.put("ok", true);
        out.put("chunked", true);
        out.put("api", 3);
        out.put("sdk", Build.VERSION.SDK_INT);
        call.resolve(out);
    }

    // ------------------------------------------------- 小文件：一次过桥

    /**
     * 小文件（前端 ≤2MB base64）走这里：整包 base64 进来，直接落公共目录。
     * 返回值带 path/uri，前端据此显示「已保存到 …」并调用 revealFile。
     */
    @PluginMethod
    public void saveFile(PluginCall call) {
        String fileName = safeName(call.getString("fileName", "baker-export.bin"));
        String mime = call.getString("mime", "application/octet-stream");
        String b64 = call.getString("base64", "");
        if (b64 == null || b64.isEmpty()) {
            call.reject("导出内容为空（0 字节），已中止以免生成空文件");
            return;
        }
        File tmp = null;
        try {
            byte[] data = Base64.decode(b64, Base64.DEFAULT);
            if (data.length == 0) {
                call.reject("导出内容解码后为 0 字节，已中止");
                return;
            }
            tmp = newTempFile();
            try (OutputStream out = new FileOutputStream(tmp)) {
                out.write(data);
            }
            Saved saved = commit(tmp, fileName, mime);
            call.resolve(savedJson(saved, data.length, true));
            toastSaved(saved);
        } catch (IllegalArgumentException e) {
            // Base64 解码失败：说明过桥的字符串被截断了，明确告诉用户而不是给个坏文件
            call.reject("导出数据在传输过程中损坏（base64 解码失败），请重试或改用「导出 JSON」");
        } catch (Exception e) {
            call.reject("保存文件失败：" + describe(e));
        } finally {
            if (tmp != null && tmp.exists()) {
                // commit 内部要么搬走要么复制，这里只清理残留
                //noinspection ResultOfMethodCallIgnored
                tmp.delete();
            }
        }
    }

    // ------------------------------------------------- 大文件：分块写入

    /**
     * 建立一次分块写入：先在缓存里开临时文件，写完（finishWrite）才搬到公共目录。
     * 这样中途失败不会在「下载」里留一个看着有、其实不完整的包。
     */
    @PluginMethod
    public void beginWrite(PluginCall call) {
        String fileName = safeName(call.getString("fileName", "baker-export.bin"));
        String mime = call.getString("mime", "application/octet-stream");
        try {
            File tmp = newTempFile();
            String id = UUID.randomUUID().toString();
            Pending p = new Pending(tmp, fileName, mime);
            pending.put(id, p);

            JSObject out = new JSObject();
            out.put("id", id);
            out.put("path", displayPathFor(fileName));
            out.put("uri", "");
            boolean mediaStore = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q;
            out.put("location", mediaStore ? "public" : "external");
            out.put("public", mediaStore);
            call.resolve(out);
        } catch (Exception e) {
            call.reject("建立导出文件失败：" + describe(e));
        }
    }

    /** 追加一块（base64）。带 sha256 时先比对，写坏了立刻报错。 */
    @PluginMethod
    public void appendChunk(PluginCall call) {
        String id = call.getString("id", "");
        Pending p = pending.get(id);
        if (p == null) {
            call.reject("写入会话已失效（可能被系统回收），请重新导出");
            return;
        }
        try {
            String b64 = call.getString("data", "");
            if (b64 == null || b64.isEmpty()) {
                call.reject("写入数据块为空");
                return;
            }
            byte[] data = Base64.decode(b64, Base64.DEFAULT);
            String expect = call.getString("sha256", "");
            if (expect != null && !expect.isEmpty()) {
                String got = hex(digestOf(data));
                if (!expect.equalsIgnoreCase(got)) {
                    call.reject("数据块校验失败（SHA-256 不一致），已中止以免写出损坏文件");
                    return;
                }
            }
            try (OutputStream out = new FileOutputStream(p.tmp, true)) {
                out.write(data);
            }
            p.bytes += data.length;
            JSObject res = new JSObject();
            res.put("ok", true);
            res.put("bytes", p.bytes);
            call.resolve(res);
        } catch (IllegalArgumentException e) {
            call.reject("数据块在传输过程中损坏（base64 解码失败）");
        } catch (Exception e) {
            call.reject("写入数据块失败：" + describe(e));
        }
    }

    /**
     * 收尾：逐块回读校验 → 搬到公共下载目录 → 返回真实落点。
     * sizeOk=false 时前端会当成失败处理（并删掉半截文件），所以这里绝不谎报成功。
     */
    @PluginMethod
    public void finishWrite(PluginCall call) {
        String id = call.getString("id", "");
        Pending p = pending.remove(id);
        if (p == null) {
            call.reject("写入会话已失效（可能被系统回收），请重新导出");
            return;
        }
        try {
            long total = p.tmp.length();
            long expectTotal = 0;
            boolean sizeOk = true;
            List<Integer> mismatches = new ArrayList<>();

            JSArray sizes = call.getArray("chunkSizes");
            JSArray hashes = call.getArray("chunkHashes");
            int n = sizes == null ? 0 : sizes.length();
            for (int i = 0; i < n; i++) {
                expectTotal += sizes.optLong(i, 0);
            }
            if (n > 0 && expectTotal != total) sizeOk = false;

            if (n > 0 && hashes != null && hashes.length() == n) {
                try (InputStream in = new FileInputStream(p.tmp)) {
                    for (int i = 0; i < n; i++) {
                        long len = sizes.optLong(i, 0);
                        String expect = hashes.optString(i, "");
                        String got = hex(digestOf(in, len));
                        if (expect != null && !expect.isEmpty() && !expect.equalsIgnoreCase(got)) {
                            mismatches.add(i);
                        }
                    }
                }
            }

            boolean verified = sizeOk && mismatches.isEmpty();
            Saved saved = commit(p.tmp, p.fileName, p.mime);
            JSObject out = savedJson(saved, total, verified);
            out.put("verified", verified);
            out.put("sizeOk", sizeOk);
            out.put("mismatches", new JSArray(mismatches));
            call.resolve(out);
            if (verified) toastSaved(saved);
        } catch (Exception e) {
            call.reject("收尾保存失败：" + describe(e));
        } finally {
            if (p.tmp.exists()) {
                //noinspection ResultOfMethodCallIgnored
                p.tmp.delete();
            }
        }
    }

    /** 中止：删掉临时文件 */
    @PluginMethod
    public void abortWrite(PluginCall call) {
        String id = call.getString("id", "");
        Pending p = pending.remove(id);
        if (p != null && p.tmp.exists()) {
            //noinspection ResultOfMethodCallIgnored
            p.tmp.delete();
        }
        JSObject out = new JSObject();
        out.put("ok", true);
        call.resolve(out);
    }

    // ------------------------------------------------------- 跳文件管理器

    /**
     * 保存完成后自动跳到文件管理器（前端在保存成功后会立刻调一次）。
     *
     * 目标是**直接落在 Download/Baker AI 并选中刚存的那个文件**，逐级降级：
     *   1. DocumentsUI 用「文件」的 document URI 打开 —— 它会进到该文件的父目录
     *      并把这一项选中（系统文件管理器唯一的"高亮"手段，没有闪烁 API）
     *   2. DocumentsUI 用 EXTRA_INITIAL_URI（tree URI）直接定位到文件夹
     *   3. 系统的「文件」入口（CATEGORY_APP_FILES）
     *   4. 直接打开这个文件（图片会进图库）
     * 全失败返回 ok=false，前端显示「已保存到 …」，用户仍能自己去找。
     *
     * ⚠️ 实测两个坑：
     *   · 把 content://…externalstorage.documents/… 直接丢给 ACTION_VIEW，
     *     vivo 会当成网页交给浏览器 → 弹「用以下方式打开」选择框。必须显式指定组件。
     *   · EXTRA_INITIAL_URI 只认 **tree** URI（buildTreeDocumentUri），给 document URI
     *     会被忽略 → 落在「下载」根目录（用户反馈"跳到了 Download 而不是 Baker AI"）。
     */
    @PluginMethod
    public void revealFile(PluginCall call) {
        final String uriStr = call.getString("uri", "");
        final String displayPath = call.getString("path", "");
        final String mime = call.getString("mime", "*/*");
        final Activity activity = getActivity();
        if (activity == null) {
            JSObject out = new JSObject();
            out.put("ok", false);
            out.put("via", "none");
            call.resolve(out);
            return;
        }

        activity.runOnUiThread(() -> {
            String via = "none";
            String rel = folderRelativePath(displayPath);
            String name = fileNameFromPath(displayPath);

            // 1) 打开"文件"本身：DocumentsUI 会进父目录 + 选中该文件（最接近"高亮"）
            if (rel != null && name != null && tryStart(activity, documentsUiFileIntent(rel, name))) {
                via = "file-select";
            }
            // 2) 只定位到文件夹
            if ("none".equals(via) && rel != null && tryStart(activity, documentsUiFolderIntent(rel))) {
                via = "folder";
            }
            if ("none".equals(via) && tryStart(activity, appFilesIntent())) {
                via = "app";
            }
            if ("none".equals(via) && uriStr != null && !uriStr.isEmpty()
                && tryStart(activity, viewIntent(uriStr, mime))) {
                via = "file";
            }

            JSObject out = new JSObject();
            out.put("ok", !"none".equals(via));
            out.put("via", via);
            call.resolve(out);
        });
    }

    /**
     * 让 DocumentsUI 打开「下载/Baker AI/<文件名>」：显式指定 FilesActivity，
     * 由它自己把父目录列出来并选中这一项（系统文件管理器没有"闪烁"接口，
     * 选中态就是能做到的最强提示）。
     */
    private static Intent documentsUiFileIntent(String relativePath, String fileName) {
        Uri doc = DocumentsContract.buildDocumentUri(
            "com.android.externalstorage.documents", "primary:" + relativePath + "/" + fileName
        );
        Intent intent = new Intent(Intent.ACTION_VIEW, doc);
        intent.setClassName("com.android.documentsui", "com.android.documentsui.files.FilesActivity");
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        return intent;
    }

    // ------------------------------------------------------------ 落盘实现

    /** 把临时文件搬进公共目录：API 29+ 优先 MediaStore，失败/低版本退应用外部目录。 */
    private Saved commit(File src, String fileName, String mime) throws IOException {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            try {
                return commitToMediaStore(src, fileName, mime);
            } catch (Exception ignored) {
                // MediaStore 在某些 ROM 上会抽风（返回 null / 写不进去）→ 退应用目录
            }
        }
        return commitToAppDir(src, fileName, mime);
    }

    /** MediaStore.Downloads：免任何存储权限，文件直接出现在「下载/Baker AI」 */
    private Saved commitToMediaStore(File src, String fileName, String mime) throws IOException {
        Context ctx = getContext();
        ContentResolver resolver = ctx.getContentResolver();
        ContentValues values = new ContentValues();
        values.put(MediaStore.MediaColumns.DISPLAY_NAME, fileName);
        values.put(MediaStore.MediaColumns.MIME_TYPE, mime);
        values.put(
            MediaStore.MediaColumns.RELATIVE_PATH,
            Environment.DIRECTORY_DOWNLOADS + "/" + SUB_DIR
        );
        values.put(MediaStore.MediaColumns.IS_PENDING, 1);

        Uri uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
        if (uri == null) throw new IOException("MediaStore 拒绝创建文件（返回空 uri）");

        boolean ok = false;
        try (InputStream in = new FileInputStream(src);
             OutputStream out = resolver.openOutputStream(uri)) {
            if (out == null) throw new IOException("MediaStore 打开输出流失败");
            copy(in, out);
            ok = true;
        } finally {
            if (!ok) {
                // 写失败就把这条空记录删掉，别在「下载」里留个 0 字节假文件
                try { resolver.delete(uri, null, null); } catch (Exception ignored) { }
            }
        }

        ContentValues done = new ContentValues();
        done.put(MediaStore.MediaColumns.IS_PENDING, 0);
        resolver.update(uri, done, null, null);

        String display = queryDisplayName(resolver, uri, fileName);
        return new Saved(
            Environment.DIRECTORY_DOWNLOADS + "/" + SUB_DIR + "/" + display,
            uri.toString(),
            true,
            "public"
        );
    }

    /** 低版本 / MediaStore 失败时的落点：应用外部目录（Android 10 及以下文件管理器可见） */
    private Saved commitToAppDir(File src, String fileName, String mime) throws IOException {
        Context ctx = getContext();
        File base = ctx.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
        String location = "external";
        if (base == null) {
            base = new File(ctx.getFilesDir(), Environment.DIRECTORY_DOWNLOADS);
            location = "internal";
        }
        File dir = new File(base, SUB_DIR);
        if (!dir.exists() && !dir.mkdirs() && !dir.isDirectory()) {
            throw new IOException("无法创建目录：" + dir.getAbsolutePath());
        }
        File target = uniqueFile(dir, fileName);

        boolean moved = src.renameTo(target);
        if (!moved) {
            try (InputStream in = new FileInputStream(src);
                 OutputStream out = new FileOutputStream(target)) {
                copy(in, out);
            }
        }

        // 让文件管理器/相册立刻能索引到（低版本不扫就看不到）
        try {
            MediaScannerConnection.scanFile(
                ctx, new String[]{target.getAbsolutePath()}, new String[]{mime}, null
            );
        } catch (Exception ignored) { }

        String display = "Android/data/" + ctx.getPackageName()
            + "/files/" + Environment.DIRECTORY_DOWNLOADS + "/" + SUB_DIR + "/" + target.getName();
        if ("internal".equals(location)) {
            display = "应用内部存储/" + Environment.DIRECTORY_DOWNLOADS + "/" + SUB_DIR + "/" + target.getName();
        }
        // Android 7+ 不能把 file:// 交给别的 App，换成自家 FileProvider 的 content://
        Uri shareUri = shareUriFor(target);
        return new Saved(display, (shareUri != null ? shareUri : Uri.fromFile(target)).toString(), false, location);
    }

    // -------------------------------------------------------------- 小工具

    private File newTempFile() throws IOException {
        File dir = new File(getContext().getCacheDir(), "savefile");
        if (!dir.exists() && !dir.mkdirs() && !dir.isDirectory()) {
            throw new IOException("无法创建临时目录");
        }
        return new File(dir, UUID.randomUUID().toString() + ".part");
    }

    private JSObject savedJson(Saved saved, long bytes, boolean verified) {
        JSObject out = new JSObject();
        out.put("bytes", bytes);
        out.put("path", saved.path);
        out.put("uri", saved.uri);
        out.put("location", saved.location);
        out.put("public", saved.isPublic);
        out.put("sizeOk", verified);
        out.put("verified", verified);
        return out;
    }

    /** 保存成功后提示落点（跳文件管理器失败时，这条 Toast 就是唯一线索） */
    private void toastSaved(Saved saved) {
        Activity activity = getActivity();
        if (activity == null) return;
        final String text = "已保存到 " + saved.path;
        activity.runOnUiThread(() -> {
            try {
                Toast.makeText(activity, text, Toast.LENGTH_LONG).show();
            } catch (Exception ignored) { }
        });
    }

    /** 预测落点（beginWrite 时还没有真实文件名，只能给个大概） */
    private String displayPathFor(String fileName) {
        return Environment.DIRECTORY_DOWNLOADS + "/" + SUB_DIR + "/" + fileName;
    }

    private static String queryDisplayName(ContentResolver resolver, Uri uri, String fallback) {
        Cursor c = null;
        try {
            c = resolver.query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null);
            if (c != null && c.moveToFirst()) {
                String name = c.getString(0);
                if (name != null && !name.isEmpty()) return name;
            }
        } catch (Exception ignored) {
        } finally {
            if (c != null) c.close();
        }
        return fallback;
    }

    private static File uniqueFile(File dir, String fileName) {
        File f = new File(dir, fileName);
        if (!f.exists()) return f;
        String base = fileName;
        String ext = "";
        int dot = fileName.lastIndexOf('.');
        if (dot > 0) {
            base = fileName.substring(0, dot);
            ext = fileName.substring(dot);
        }
        for (int i = 1; i < 1000; i++) {
            File candidate = new File(dir, base + " (" + i + ")" + ext);
            if (!candidate.exists()) return candidate;
        }
        return new File(dir, base + "-" + System.currentTimeMillis() + ext);
    }

    /** 去掉路径分隔符等非法字符，防止前端传进来的名字越界写文件 */
    private static String safeName(String raw) {
        if (raw == null || raw.trim().isEmpty()) return "baker-export.bin";
        String name = raw.replace('\\', '_').replace('/', '_').trim();
        if (name.length() > 120) {
            String ext = "";
            int dot = name.lastIndexOf('.');
            if (dot > 0) ext = name.substring(dot);
            name = name.substring(0, 120 - ext.length()) + ext;
        }
        return name;
    }

    private static void copy(InputStream in, OutputStream out) throws IOException {
        byte[] buf = new byte[64 * 1024];
        int read;
        while ((read = in.read(buf)) > 0) {
            out.write(buf, 0, read);
        }
        out.flush();
    }

    private static byte[] digestOf(byte[] data) throws NoSuchAlgorithmException {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        return md.digest(data);
    }

    /** 从流里读恰好 len 个字节算 SHA-256（逐块回读校验用） */
    private static byte[] digestOf(InputStream in, long len) throws NoSuchAlgorithmException, IOException {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] buf = new byte[64 * 1024];
        long left = len;
        while (left > 0) {
            int want = (int) Math.min(buf.length, left);
            int read = in.read(buf, 0, want);
            if (read <= 0) break;
            md.update(buf, 0, read);
            left -= read;
        }
        return md.digest();
    }

    private static String hex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            sb.append(String.format(Locale.US, "%02x", b));
        }
        return sb.toString();
    }

    private static String describe(Exception e) {
        String msg = e.getMessage();
        return (msg == null || msg.isEmpty()) ? e.getClass().getSimpleName() : msg;
    }

    /** 「下载/Baker AI/x.zip」→「Download/Baker AI」，供 DocumentsUI 定位文件夹 */
    private static String folderRelativePath(String displayPath) {
        if (displayPath == null || displayPath.isEmpty()) return null;
        String p = displayPath.replace('\\', '/');
        String downloadPrefix = Environment.DIRECTORY_DOWNLOADS + "/" + SUB_DIR;
        if (p.startsWith(downloadPrefix)) {
            return "Download/" + SUB_DIR;
        }
        int idx = p.indexOf("/" + Environment.DIRECTORY_DOWNLOADS + "/" + SUB_DIR);
        if (idx >= 0) return "Download/" + SUB_DIR;
        return null;
    }

    /** 「Download/Baker AI/x.zip」→「x.zip」（去掉最后一段） */
    private static String fileNameFromPath(String displayPath) {
        if (displayPath == null || displayPath.isEmpty()) return null;
        String p = displayPath.replace('\\', '/');
        int slash = p.lastIndexOf('/');
        if (slash < 0 || slash == p.length() - 1) return null;
        return p.substring(slash + 1);
    }

    /**
     * 用 DocumentsUI 的「文件」界面打开指定文件夹。
     *
     * ⚠️ EXTRA_INITIAL_URI 只认 **tree** URI：给 buildDocumentUri 造的 document URI
     * 会被忽略，结果落在「下载」根目录（真机反馈"跳到的是 Download 而不是 Baker AI"）。
     */
    private static Intent documentsUiFolderIntent(String relativePath) {
        Uri folderTree = DocumentsContract.buildTreeDocumentUri(
            "com.android.externalstorage.documents", "primary:" + relativePath
        );
        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setClassName("com.android.documentsui", "com.android.documentsui.files.FilesActivity");
        intent.putExtra(DocumentsContract.EXTRA_INITIAL_URI, folderTree);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        return intent;
    }

    /** 直接拉起系统「文件」App（部分 ROM 的文件夹 URI 不认，这是最稳的兜底） */
    private static Intent appFilesIntent() {
        Intent intent = new Intent(Intent.ACTION_MAIN);
        intent.addCategory(Intent.CATEGORY_APP_FILES);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return intent;
    }

    /** 用别的 App 打开这个文件（图片走图库，一眼能看到刚导出的图） */
    private static Intent viewIntent(String uriStr, String mime) {
        Uri uri;
        try {
            uri = Uri.parse(uriStr);
        } catch (Exception e) {
            return null;
        }
        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setDataAndType(uri, mime == null || mime.isEmpty() ? "*/*" : mime);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        return intent;
    }

    private static boolean tryStart(Activity activity, Intent intent) {
        if (intent == null) return false;
        try {
            activity.startActivity(intent);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * 应用外部目录里的文件不能用 file:// 直接给别的 App（Android 7+ 会抛
     * FileUriExposedException），这里统一换成我们自己 FileProvider 的 content://。
     */
    @SuppressWarnings("unused")
    private Uri shareUriFor(File file) {
        try {
            return FileProvider.getUriForFile(
                getContext(), getContext().getPackageName() + ".fileprovider", file
            );
        } catch (Exception e) {
            return null;
        }
    }
}

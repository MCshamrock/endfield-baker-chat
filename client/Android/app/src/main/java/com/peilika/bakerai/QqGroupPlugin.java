package com.peilika.bakerai;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * QqGroup —— 「关于页 → 加入QQ群」的跳转通道。
 *
 * 网页里那两条群链接是 qun.qq.com 的 H5 分享页，在 WebView 里点开只会把 H5
 * 加载到应用自己身上（iOS/安卓都一样），用户还得再点一次「打开QQ」。
 * 这里直接用 mqqapi 协议唤起 QQ 的群名片，点到即进群；没装 QQ 再退回 H5。
 */
@CapacitorPlugin(name = "QqGroup")
public class QqGroupPlugin extends Plugin {

    /** QQ 群名片：src_type=internal + card_type=group 是 QQ 官方认的组合 */
    private static final String QQ_CARD = "mqqapi://card/show_pslcard"
        + "?src_type=internal&version=1&uin=%s&card_type=group&source=qrcode";

    /**
     * 打开群名片。
     * @param uin 群号（纯数字）
     * @param url 兜底的 H5 分享页（QQ 没装时用）
     */
    @PluginMethod
    public void open(PluginCall call) {
        final String uin = call.getString("uin", "");
        final String fallback = call.getString("url", "");
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
            if (uin != null && !uin.isEmpty() && tryStart(activity, qqIntent(uin))) {
                via = "qq";
            } else if (fallback != null && !fallback.isEmpty() && tryStart(activity, webIntent(fallback))) {
                via = "web";
            }
            JSObject out = new JSObject();
            out.put("ok", !"none".equals(via));
            out.put("via", via);
            call.resolve(out);
        });
    }

    private static Intent qqIntent(String uin) {
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(String.format(QQ_CARD, uin)));
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return intent;
    }

    private static Intent webIntent(String url) {
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return intent;
    }

    private static boolean tryStart(Activity activity, Intent intent) {
        try {
            activity.startActivity(intent);
            return true;
        } catch (Exception e) {
            // 没装 QQ / 没有浏览器 / 协议被拦：交给下一级兜底
            return false;
        }
    }
}

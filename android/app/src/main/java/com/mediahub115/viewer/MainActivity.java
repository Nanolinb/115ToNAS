package com.mediahub115.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.InputType;
import android.view.KeyEvent;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

/**
 * 115影库 · 观影端 TV 壳：
 * - WebView 加载 NAS 上的观影页（海报墙/搜索/过滤）
 * - 播放走内嵌 ExoPlayer（应用内全屏，电视/投影硬解，可选音轨/字幕，
 *   音轨解不了自动回落服务端 AAC 转码），不跳外部 App
 */
public class MainActivity extends Activity {

    private static final String PREFS = "mediahub";
    private static final String KEY_SERVER = "server";
    private static final String DEFAULT_SERVER = "http://192.168.1.107:8115";

    private WebView web;
    private SharedPreferences sp;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        sp = getSharedPreferences(PREFS, MODE_PRIVATE);

        web = new WebView(this);
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setTextZoom(100);

        web.addJavascriptInterface(new NativeBridge(), "MediaHubNative");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    showServerDialog("连不上服务器，请检查 NAS 是否在线、地址是否正确");
                }
            }
        });

        String server = sp.getString(KEY_SERVER, "");
        if (server.isEmpty()) {
            showServerDialog(null);
        } else {
            web.loadUrl(server + "/");
        }
    }

    /** 服务器地址设置弹窗（首次启动 / 连接失败 / 按菜单键时弹出） */
    private void showServerDialog(String err) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(sp.getString(KEY_SERVER, DEFAULT_SERVER));
        input.setSelection(input.getText().length());
        new AlertDialog.Builder(this)
                .setTitle("NAS 服务器地址")
                .setMessage(err == null
                        ? "首次使用，请输入 115影库 在 NAS 上的地址："
                        : err + "\n\n请确认地址：")
                .setView(input)
                .setPositiveButton("连接", (d, w) -> {
                    String url = input.getText().toString().trim();
                    if (!url.startsWith("http")) {
                        url = "http://" + url;
                    }
                    sp.edit().putString(KEY_SERVER, url).apply();
                    web.loadUrl(url + "/");
                })
                .setNegativeButton("取消", null)
                .setCancelable(false)
                .show();
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        // 遥控器「菜单」键：重新设置服务器地址
        if (keyCode == KeyEvent.KEYCODE_MENU) {
            showServerDialog(null);
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    /** 网页通过 window.MediaHubNative 调用的原生能力 */
    public class NativeBridge {

        /**
         * 内嵌 ExoPlayer 播放（应用内全屏）：电视/投影硬件解码，
         * 遥控器可选音轨/字幕；音轨解不了自动回落服务端 AAC 转码。
         * subsJson: [{"label","url","ext"}]，可为 null。
         */
        @JavascriptInterface
        public void play(final String url, final String title, final String subsJson) {
            runOnUiThread(() -> {
                Intent i = new Intent(MainActivity.this, PlayerActivity.class);
                i.putExtra("url", url);
                i.putExtra("title", title);
                i.putExtra("subs", subsJson);
                startActivity(i);
            });
        }
    }
}

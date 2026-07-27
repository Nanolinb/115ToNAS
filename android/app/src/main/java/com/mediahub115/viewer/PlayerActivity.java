package com.mediahub115.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.Toast;

import androidx.annotation.OptIn;
import androidx.media3.common.C;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MimeTypes;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.common.Tracks;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.datasource.DefaultHttpDataSource;
import androidx.media3.exoplayer.DefaultRenderersFactory;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.Renderer;
import androidx.media3.exoplayer.mediacodec.MediaCodecInfo;
import androidx.media3.exoplayer.mediacodec.MediaCodecSelector;
import androidx.media3.exoplayer.mediacodec.MediaCodecUtil.DecoderQueryException;
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory;
import androidx.media3.exoplayer.video.VideoRendererEventListener;
import androidx.media3.exoplayer.util.EventLogger;
import androidx.media3.ui.PlayerView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 内嵌播放器：ExoPlayer 硬件解码（电视/投影 MediaCodec 可解 HEVC，
 * 浏览器 WebView 解不了的在这里也能播），应用内完成，不跳外部 App。
 *
 * - 播放/暂停/进度条用 ExoPlayer 原生控制器；额外补一排电视需要的按钮：
 *   音轨、字幕、选集（剧集才显示），随控制器一起出现/隐藏
 * - 外挂字幕：服务端 srt→vtt 直挂，ass 走 ExoPlayer 自带 SSA 解析
 * - 音轨解码失败（无 DTS/TrueHD 授权）→ 自动回落服务端 ?audio=aac 转码流
 * - 遥控器左右键快退/快进 10 秒（控制器隐藏时）
 * - 续播：每 5 秒记录进度到 SharedPreferences，再打开时弹窗问「继续播放/从头开始」
 * - 选集切集：按影片 id 向服务端拉播放信息（带 WebView 同源 Cookie，观影密码下也能播）
 */
@OptIn(markerClass = UnstableApi.class)
public class PlayerActivity extends Activity {

    private static final String PREFS = "resume";
    private static final Pattern MEDIA_ID = Pattern.compile("/api/stream/(\\d+)");

    private ExoPlayer player;
    private PlayerView view;
    private View menuBar;     // 底部半透明菜单条（音轨/字幕/选集/关闭）
    private String url;
    private String title;
    private String subsJson;
    private String epsJson;   // [{"id","label","name"}]，剧集才有
    private String cookie;    // WebView 同源 Cookie（观影密码鉴权用）
    private boolean aacFallback;  // 已切到服务端 AAC 转码流
    private boolean trackChecked; // 首次 onTracksChanged 已检测
    private String posKey;        // 续播存档键（按影片 id，playlink 令牌每次变，不能按 URL）
    private String mediaId;       // 影片 id（AAC 回退/切集时用）
    private String apiBase;       // 服务器 origin（playUrl 里 /api/ 之前的部分）

    private final Handler handler = new Handler(Looper.getMainLooper());
    // 自动隐藏菜单条时把焦点交回播放画面：否则焦点残留在看不见的按钮上，
    // 再按确认键会点到隐形按钮（最坏直接「关闭」退出播放）
    private final Runnable menuHider = () -> {
        menuBar.setVisibility(View.GONE);
        menuBar.clearFocus();
        view.requestFocus();
    };
    private final Runnable saver = new Runnable() {
        @Override
        public void run() {
            savePos();
            handler.postDelayed(this, 5000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        url = getIntent().getStringExtra("url");
        title = getIntent().getStringExtra("title");
        subsJson = getIntent().getStringExtra("subs");
        epsJson = getIntent().getStringExtra("eps");
        cookie = getIntent().getStringExtra("cookie");
        int apiIdx = url == null ? -1 : url.indexOf("/api/");
        apiBase = apiIdx > 0 ? url.substring(0, apiIdx) : "";

        // letterbox 区域必须纯黑：默认主题窗口底色是灰的，宽银幕片上下灰边刺眼
        getWindow().getDecorView().setBackgroundColor(Color.BLACK);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        // 原生控制器：播放/暂停/进度条都是 ExoPlayer 自带的那套
        view = new PlayerView(this);
        view.setUseController(true);
        view.setControllerShowTimeoutMs(5000);
        view.setBackgroundColor(Color.BLACK);
        view.setShutterBackgroundColor(Color.BLACK);
        // 字幕样式：去掉纯黑底块，改白字黑描边（对画面遮挡更少）
        if (view.getSubtitleView() != null) {
            view.getSubtitleView().setStyle(new androidx.media3.ui.CaptionStyleCompat(
                    Color.WHITE, Color.TRANSPARENT, Color.TRANSPARENT,
                    androidx.media3.ui.CaptionStyleCompat.EDGE_TYPE_OUTLINE,
                    Color.BLACK, null));
        }
        root.addView(view, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        menuBar = buildMenuBar();
        FrameLayout.LayoutParams barLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT);
        barLp.gravity = Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL;
        barLp.setMargins(0, 0, 0, 40);
        root.addView(menuBar, barLp);
        setContentView(root);

        // 底部菜单条：MENU / 方向键下 唤出，返回或 6 秒无操作隐藏
        menuBar.setVisibility(View.GONE);

        // 带 Cookie 的 HTTP 源：观影端设了密码时，/api/stream 也要鉴权
        // 4K 片源 OOM 修复：默认缓冲按 50 秒算，4K 高码率要吃掉几百 MB 堆，
        // 这台老电视默认堆 128MB，播放线程直接 OutOfMemoryError（白屏退出）。
        // 压到 24MB 硬顶 + 10~20 秒时长窗（局域网传得起，4K 约 8 秒数据量）
        androidx.media3.exoplayer.DefaultLoadControl loadControl =
                new androidx.media3.exoplayer.DefaultLoadControl.Builder()
                        .setBufferDurationsMs(10_000, 20_000, 1_500, 3_000)
                        .setTargetBufferBytes(24 * 1024 * 1024)
                        .build();
        ExoPlayer.Builder builder = new ExoPlayer.Builder(this)
                .setLoadControl(loadControl);
        // Dolby Vision 解码策略：优先用设备原生 DV 解码器（极米 R10 等有杜比
        // 认证的机器能完整解 DV）；只有设备没有任何 DV 解码器时（小米老电视这类
        // 老 Amlogic 芯片，ExoPlayer 会整条放弃视频轨 → 黑屏有声），才把
        // video/dolby-vision 重定向到 HEVC 解码器解基础层（DV 增强层被丢弃，
        // 画面退回 HDR10），至少能看。
        final MediaCodecSelector dvFallbackSelector = new MediaCodecSelector() {
            @Override
            public List<MediaCodecInfo> getDecoderInfos(String mimeType,
                    boolean requiresSecureDecoder, boolean requiresTunnelingDecoder)
                    throws DecoderQueryException {
                if (MimeTypes.VIDEO_DOLBY_VISION.equals(mimeType)) {
                    List<MediaCodecInfo> dv = MediaCodecSelector.DEFAULT.getDecoderInfos(
                            mimeType, requiresSecureDecoder, requiresTunnelingDecoder);
                    if (!dv.isEmpty()) {
                        return dv; // 设备有杜比认证解码器，走原生 DV
                    }
                    return MediaCodecSelector.DEFAULT.getDecoderInfos(
                            MimeTypes.VIDEO_H265, requiresSecureDecoder, requiresTunnelingDecoder);
                }
                return MediaCodecSelector.DEFAULT.getDecoderInfos(
                        mimeType, requiresSecureDecoder, requiresTunnelingDecoder);
            }
        };
        builder.setRenderersFactory(new DefaultRenderersFactory(this) {
            @Override
            protected void buildVideoRenderers(Context context, int extensionRendererMode,
                    MediaCodecSelector mediaCodecSelector, boolean enableDecoderFallback,
                    Handler eventHandler, VideoRendererEventListener eventListener,
                    long allowedVideoJoiningTimeMs, ArrayList<Renderer> out) {
                super.buildVideoRenderers(context, extensionRendererMode, dvFallbackSelector,
                        true, eventHandler, eventListener, allowedVideoJoiningTimeMs, out);
            }
        }.setEnableDecoderFallback(true));
        if (cookie != null && !cookie.isEmpty()) {
            Map<String, String> headers = new HashMap<>();
            headers.put("Cookie", cookie);
            builder.setMediaSourceFactory(new DefaultMediaSourceFactory(
                    new DefaultHttpDataSource.Factory().setDefaultRequestProperties(headers)));
        }
        player = builder.build();
        // 默认轨道偏好：音轨 英 > 日 > 中；字幕 中文优先（简中 > 繁中）。
        // ExoPlayer 会按 639-1/639-2/639-3 归一化匹配内外挂轨道
        player.setTrackSelectionParameters(
                player.getTrackSelectionParameters().buildUpon()
                        .setPreferredAudioLanguages("en", "eng", "ja", "jpn",
                                "zh", "cmn", "zho", "chi")
                        .setPreferredTextLanguages("zh-Hans", "zh-Hant",
                                "zh", "cmn", "zho", "chi")
                        .build());
        // 预热反射：media3 的 AudioTrackPositionTracker 会在播放线程上反射
        // AudioTrack.getLatency，这台 Android 6 机器的 ART 有 native bug，
        // 偶发 SIGSEGV 直接杀进程（用户看到的"播几秒白屏退出"）。
        // 主线程先调一次让类提前完成链接，避开播放线程上的竞态
        try {
            android.media.AudioTrack.class.getMethod("getLatency", (Class<?>[]) null);
        } catch (Throwable ignore) {
            // 老系统没有该方法属正常，预热只为触发类链接
        }
        if (BuildConfig.DEBUG) {
            player.addAnalyticsListener(new EventLogger("MediaHub"));
        }
        view.setPlayer(player);
        player.addListener(new Player.Listener() {
            @Override
            public void onPlayerError(PlaybackException error) {
                if (!aacFallback) {
                    switchToAac("解码失败，已切换服务端 AAC 转码");
                } else {
                    Toast.makeText(PlayerActivity.this,
                            "播放失败: " + error.getMessage(), Toast.LENGTH_LONG).show();
                }
            }

            @Override
            public void onTracksChanged(Tracks tracks) {
                if (trackChecked || aacFallback) {
                    return;
                }
                trackChecked = true;
                // 文件有音轨但设备一条都选不中（无授权解码器）→ 播放会无声，转 AAC
                boolean hasAudio = false, selectedAudio = false;
                for (Tracks.Group g : tracks.getGroups()) {
                    if (g.getType() == C.TRACK_TYPE_AUDIO) {
                        hasAudio = true;
                        if (g.isSelected()) {
                            selectedAudio = true;
                        }
                    }
                }
                if (hasAudio && !selectedAudio) {
                    switchToAac("设备不支持该音轨，已切换服务端 AAC 转码");
                }
            }
        });

        handler.postDelayed(saver, 5000);
        applyMediaRef(url, title, subsJson);
        startPlayback();
    }

    /** 底部半透明菜单条：音轨 / 字幕 / 选集（剧集才有）/ 关闭，遥控器左右键选择 */
    private View buildMenuBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(24, 14, 24, 14);
        android.graphics.drawable.GradientDrawable bg = new android.graphics.drawable.GradientDrawable();
        bg.setColor(0xB3000000);
        bg.setCornerRadius(48);
        bar.setBackground(bg);
        Button btnAudio = barButton("音轨");
        btnAudio.setOnClickListener(v -> showTrackDialog(C.TRACK_TYPE_AUDIO, "选择音轨"));
        bar.addView(btnAudio, btnLp());
        Button btnSubs = barButton("字幕");
        btnSubs.setOnClickListener(v -> showTrackDialog(C.TRACK_TYPE_TEXT, "选择字幕"));
        bar.addView(btnSubs, btnLp());
        boolean hasEps;
        try {
            hasEps = new JSONArray(epsJson == null ? "[]" : epsJson).length() > 0;
        } catch (Exception e) {
            hasEps = false;
        }
        if (hasEps) {
            Button btnEps = barButton("选集");
            btnEps.setOnClickListener(v -> showEpisodeDialog());
            bar.addView(btnEps, btnLp());
        }
        Button btnClose = barButton("关闭");
        btnClose.setOnClickListener(v -> finish());
        bar.addView(btnClose, btnLp());
        return bar;
    }

    private void showMenuBar() {
        view.hideController();
        menuBar.setVisibility(View.VISIBLE);
        View first = ((LinearLayout) menuBar).getChildAt(0);
        if (first != null) {
            first.requestFocus();
        }
        pokeMenuBar();
    }

    private void pokeMenuBar() {
        handler.removeCallbacks(menuHider);
        handler.postDelayed(menuHider, 6000);
    }

    /** 裸画面下按返回：确认退出弹窗，默认焦点在「取消」防误触 */
    private void confirmExit() {
        AlertDialog d = new AlertDialog.Builder(this)
                .setTitle("退出播放")
                .setPositiveButton("确认退出", (dlg, w) -> finish())
                .setNegativeButton("取消", null)
                .show();
        d.getButton(AlertDialog.BUTTON_NEGATIVE).requestFocus();
    }

    private Button barButton(String label) {
        Button b = new Button(this);
        b.setText(label);
        b.setTextColor(new android.content.res.ColorStateList(
                new int[][]{
                        new int[]{android.R.attr.state_focused},
                        new int[]{}},
                new int[]{0xFF111111, Color.WHITE}));
        b.setTextSize(14);
        // 焦点/按下态底色：默认半透黑，选中变亮（琥珀黄底黑字），按下稍暗一档
        android.graphics.drawable.GradientDrawable def = new android.graphics.drawable.GradientDrawable();
        def.setColor(0x55000000); def.setCornerRadius(40);
        android.graphics.drawable.GradientDrawable foc = new android.graphics.drawable.GradientDrawable();
        foc.setColor(0xE6FFCC00); foc.setCornerRadius(40);
        android.graphics.drawable.GradientDrawable pre = new android.graphics.drawable.GradientDrawable();
        pre.setColor(0x99CC9900); pre.setCornerRadius(40);
        android.graphics.drawable.StateListDrawable states = new android.graphics.drawable.StateListDrawable();
        states.addState(new int[]{android.R.attr.state_pressed}, pre);
        states.addState(new int[]{android.R.attr.state_focused}, foc);
        states.addState(new int[]{}, def);
        b.setBackgroundDrawable(states);
        b.setPadding(28, 4, 28, 4);
        b.setMinHeight(0);
        b.setMinimumHeight(0);
        b.setFocusable(true);
        return b;
    }

    private LinearLayout.LayoutParams btnLp() {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(16, 0, 0, 0);
        return lp;
    }

    /**
     * 轨道选择弹窗（音轨/字幕通用）：单选列表，点中即生效并关闭——
     * 遥控器没有鼠标，ExoPlayer 自带的 TrackSelectionDialog 选完还要
     * 一路按到「确认」按钮，长字幕列表下基本没法用。
     */
    private void showTrackDialog(int type, String titleText) {
        // 弹窗打开时收起菜单条：否则弹窗抢焦点前的按键会落到菜单条按钮上
        handler.removeCallbacks(menuHider);
        menuBar.setVisibility(View.GONE);
        Tracks current = player.getCurrentTracks();
        List<Tracks.Group> groups = new ArrayList<>();
        for (Tracks.Group g : current.getGroups()) {
            if (g.getType() == type && g.length > 0) {
                groups.add(g);
            }
        }
        if (groups.isEmpty()) {
            Toast.makeText(this, type == C.TRACK_TYPE_TEXT
                    ? "没有可选字幕" : "没有可选音轨", Toast.LENGTH_SHORT).show();
            return;
        }
        final boolean isText = type == C.TRACK_TYPE_TEXT;
        final int offset = isText ? 1 : 0; // 字幕菜单第 0 项是「关闭字幕」
        String[] items = new String[groups.size() + offset];
        if (isText) {
            items[0] = "关闭字幕";
        }
        for (int i = 0; i < groups.size(); i++) {
            androidx.media3.common.Format f = groups.get(i).getTrackFormat(0);
            StringBuilder sb = new StringBuilder();
            sb.append(isText ? "字幕 " : "音轨 ").append(i + 1);
            List<String> bits = new ArrayList<>();
            if (f.language != null && !f.language.equals("und")) {
                bits.add(f.language);
            }
            if (f.label != null && !f.label.isEmpty()) {
                bits.add(f.label);
            }
            if (f.codecs != null) {
                bits.add(f.codecs);
            }
            if (!bits.isEmpty()) {
                sb.append(" · ").append(android.text.TextUtils.join(" / ", bits));
            }
            // 当前生效的轨道打个勾
            if (groups.get(i).isSelected()) {
                sb.append("  ✓");
            }
            items[i + offset] = sb.toString();
        }
        new AlertDialog.Builder(this)
                .setTitle(titleText)
                .setItems(items, (d, which) -> {
                    androidx.media3.common.TrackSelectionParameters.Builder b =
                            player.getTrackSelectionParameters().buildUpon();
                    if (isText && which == 0) {
                        b.setTrackTypeDisabled(C.TRACK_TYPE_TEXT, true);
                        b.clearOverridesOfType(C.TRACK_TYPE_TEXT);
                    } else {
                        b.setTrackTypeDisabled(type, false);
                        b.setOverrideForType(new androidx.media3.common.TrackSelectionOverride(
                                groups.get(which - offset).getMediaTrackGroup(), 0));
                    }
                    player.setTrackSelectionParameters(b.build());
                })
                .show();
    }

    /** 选集弹窗：列出同剧各集，点选后向服务端拉该集播放信息并切换 */
    private void showEpisodeDialog() {
        try {
            JSONArray eps = new JSONArray(epsJson == null ? "[]" : epsJson);
            if (eps.length() == 0) {
                return;
            }
            // 同 showTrackDialog：弹窗打开时收起菜单条，避免按键穿透
            handler.removeCallbacks(menuHider);
            menuBar.setVisibility(View.GONE);
            String[] items = new String[eps.length()];
            final long[] ids = new long[eps.length()];
            for (int i = 0; i < eps.length(); i++) {
                JSONObject o = eps.getJSONObject(i);
                ids[i] = o.getLong("id");
                items[i] = o.optString("label") + "  " + o.optString("name");
            }
            new AlertDialog.Builder(this)
                    .setTitle("选集")
                    .setItems(items, (d, which) -> switchEpisode(ids[which]))
                    .show();
        } catch (Exception ignore) {
            // 选集数据异常不阻断播放
        }
    }

    private void switchEpisode(final long epId) {
        if (String.valueOf(epId).equals(mediaId)) {
            return;
        }
        Toast.makeText(this, "正在加载选集…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                JSONObject d = new JSONObject(httpGet(apiBase + "/api/media/" + epId));
                JSONObject pl = new JSONObject(httpGet(
                        apiBase + "/api/media/" + epId + "/playlink"));
                String newUrl = apiBase + pl.getString("url");
                String newTitle = d.optString("filename", title);
                JSONArray subs = d.optJSONArray("subs");
                JSONArray subCfgs = new JSONArray();
                if (subs != null) {
                    for (int i = 0; i < subs.length(); i++) {
                        JSONObject t = subs.getJSONObject(i);
                        JSONObject cfg = new JSONObject();
                        cfg.put("label", t.optString("label",
                                t.optString("lang", "字幕 " + (i + 1))));
                        cfg.put("url", apiBase + "/api/subtitle/" + epId + "/" + i);
                        cfg.put("ext", "vtt");
                        subCfgs.put(cfg);
                    }
                }
                final String u = newUrl, t2 = newTitle, s = subCfgs.toString();
                runOnUiThread(() -> {
                    savePos(); // 旧集进度先落盘
                    applyMediaRef(u, t2, s);
                    startPlayback();
                });
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(this,
                        "选集加载失败", Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    /** 切换影片引用：重置解码/续播状态 */
    private void applyMediaRef(String newUrl, String newTitle, String newSubs) {
        url = newUrl;
        title = newTitle;
        subsJson = newSubs;
        aacFallback = false;
        trackChecked = false;
        Matcher m = MEDIA_ID.matcher(url == null ? "" : url);
        mediaId = m.find() ? m.group(1) : null;
        posKey = mediaId == null ? null : "pos_" + mediaId;
        setTitle(title == null ? "" : title);
    }

    /** 起播入口：有续播存档先问，没有就从零开始 */
    private void startPlayback() {
        long saved = posKey == null ? 0
                : getSharedPreferences(PREFS, MODE_PRIVATE).getLong(posKey, 0);
        if (saved > 30_000) {
            askResume(saved);
        } else {
            load(url, 0, 0);
        }
    }

    private void askResume(long savedMs) {
        new AlertDialog.Builder(this)
                .setTitle("续播")
                .setMessage("上次看到 " + fmt(savedMs) + "，是否从上次的位置开始？")
                .setPositiveButton("继续播放", (d, w) -> load(url, savedMs, 0))
                .setNegativeButton("从头开始", (d, w) -> {
                    if (posKey != null) {
                        getSharedPreferences(PREFS, MODE_PRIVATE)
                                .edit().remove(posKey).apply();
                    }
                    load(url, 0, 0);
                })
                .setCancelable(false)
                .show();
    }

    private static String fmt(long ms) {
        long s = Math.max(0, ms / 1000);
        long h = s / 3600, m = (s % 3600) / 60, ss = s % 60;
        return h > 0 ? String.format("%d:%02d:%02d", h, m, ss)
                     : String.format("%d:%02d", m, ss);
    }

    /** 30 秒以内不记；快播完（剩不到 60 秒）自动清档，下次从头播。 */
    private void savePos() {
        if (player == null || posKey == null) {
            return;
        }
        long pos = player.getCurrentPosition();
        long dur = player.getDuration();
        SharedPreferences.Editor e = getSharedPreferences(PREFS, MODE_PRIVATE).edit();
        if (dur > 0 && dur - pos <= 60_000) {
            e.remove(posKey);
        } else if (pos > 30_000) {
            e.putLong(posKey, pos);
        } else {
            return;
        }
        e.apply();
    }

    /**
     * subOffSec：转码流的起点对齐偏移（服务端把流起点对齐到关键帧落点 V）。
     * 字幕是绝对时间轴，>0 时给字幕 URL 带 ?offset=V，服务端整体平移 cue。
     */
    private void load(String u, long startMs, long subOffSec) {
        // Activity 销毁后 AAC 回退线程的回调可能才到：player 已释放，直接丢弃
        if (player == null || isFinishing() || isDestroyed()) {
            return;
        }
        MediaItem.Builder mb = new MediaItem.Builder().setUri(u);
        List<MediaItem.SubtitleConfiguration> cfgs = new ArrayList<>();
        try {
            JSONArray arr = new JSONArray(subsJson == null ? "[]" : subsJson);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                String ext = o.optString("ext", "vtt").toLowerCase();
                String mime = (ext.equals("ass") || ext.equals("ssa"))
                        ? MimeTypes.TEXT_SSA : MimeTypes.TEXT_VTT;
                String subUrl = o.getString("url");
                if (subOffSec > 0) {
                    subUrl += (subUrl.contains("?") ? "&" : "?") + "offset=" + subOffSec;
                }
                cfgs.add(new MediaItem.SubtitleConfiguration.Builder(
                        Uri.parse(subUrl))
                        .setMimeType(mime)
                        .setLabel(o.optString("label", "字幕 " + (i + 1)))
                        // 外挂字幕按 label 推断语言标签：默认字幕偏好（简中>繁中）才能匹配
                        .setLanguage(guessSubLang(o.optString("label", "")))
                        .build());
            }
        } catch (Exception ignore) {
            // 字幕 JSON 异常不阻断播放
        }
        if (!cfgs.isEmpty()) {
            mb.setSubtitleConfigurations(cfgs);
        }
        player.setMediaItem(mb.build(), startMs);
        player.prepare();
        player.play();
    }

    /** 按字幕文件名/标签推断 BCP-47 语言：简中 zh-Hans > 繁中 zh-Hant > 中文 zh */
    private static String guessSubLang(String label) {
        String l = label == null ? "" : label.toLowerCase(java.util.Locale.ROOT);
        if (l.matches(".*(简体|简中|chs|gb2312|gbk|sc[.&_ -]|zh[-_.]?hans|zh[-_.]?cn).*")) {
            return "zh-Hans";
        }
        if (l.matches(".*(繁体|繁中|cht|big5|tc[.&_ -]|zh[-_.]?hant|zh[-_.]?tw|zh[-_.]?hk).*")) {
            return "zh-Hant";
        }
        if (l.matches(".*(中|双语|chi|zho|cmn|chinese).*")) {
            return "zh";
        }
        if (l.matches(".*(英|eng|english|[.&_ -]en[.&_ -]).*")) {
            return "en";
        }
        if (l.matches(".*(日|jpn|japanese|[.&_ -]ja[.&_ -]).*")) {
            return "ja";
        }
        return "und";
    }

    private void switchToAac(String msg) {
        aacFallback = true;
        final long posSec = Math.max(0, player.getCurrentPosition()) / 1000;
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
        // 转码流视频整包拷贝只能落关键帧：服务端把流起点对齐到落点 V（≤ posSec）。
        // 先问 /api/stream-prep 拿 V，起播位置与字幕都按 V 换算，否则进度/字幕错位
        new Thread(() -> {
            long v = posSec;
            if (mediaId != null) {
                try {
                    v = (long) new JSONObject(httpGet(apiBase + "/api/stream-prep/"
                            + mediaId + "?t=" + posSec)).getDouble("start");
                } catch (Exception ignore) {
                    // 拿不到就按 posSec 起，最多错一个 GOP 的显示位置
                }
            }
            final long start = v;
            runOnUiThread(() -> {
                String sep = url.contains("?") ? "&" : "?";
                load(url + sep + "audio=aac&t=" + posSec,
                        Math.max(0, (posSec - start) * 1000), start);
            });
        }).start();
    }

    /** 带 Cookie 的 GET（观影密码鉴权）；响应体按 UTF-8 文本返回 */
    private String httpGet(String u) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(u).openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(8000);
        if (cookie != null && !cookie.isEmpty()) {
            c.setRequestProperty("Cookie", cookie);
        }
        InputStream in = c.getInputStream();
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) {
            bos.write(buf, 0, n);
        }
        in.close();
        return new String(bos.toByteArray(), "UTF-8");
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        // 底部菜单条显示时：方向键交给按钮焦点导航，返回键收起菜单条
        if (menuBar.getVisibility() == View.VISIBLE) {
            switch (keyCode) {
                case KeyEvent.KEYCODE_BACK:
                case KeyEvent.KEYCODE_MENU:
                    menuBar.setVisibility(View.GONE);
                    menuBar.clearFocus();
                    view.requestFocus();
                    return true;
                case KeyEvent.KEYCODE_DPAD_LEFT:
                case KeyEvent.KEYCODE_DPAD_RIGHT:
                case KeyEvent.KEYCODE_DPAD_UP:
                case KeyEvent.KEYCODE_DPAD_DOWN:
                    pokeMenuBar();
                    return super.onKeyDown(keyCode, event);
                default:
                    return super.onKeyDown(keyCode, event);
            }
        }
        switch (keyCode) {
            case KeyEvent.KEYCODE_BACK:
                // 逐层退出：播放控制条（进度条）还亮着时先收控制条；
                // 什么浮层都没有时，弹「确认退出/取消」，不直接退回影片墙
                if (view.isControllerFullyVisible()) {
                    view.hideController();
                    return true;
                }
                confirmExit();
                return true;
            case KeyEvent.KEYCODE_MENU:
                // MENU：任何时候都从底部弹出半透明菜单条（字幕/选集/音轨）
                showMenuBar();
                return true;
            case KeyEvent.KEYCODE_CAPTIONS:
                showTrackDialog(C.TRACK_TYPE_TEXT, "选择字幕");
                return true;
            case KeyEvent.KEYCODE_DPAD_DOWN:
                // 控制条亮着时，方向键交给它做焦点导航（可下选到进度条）；
                // 否则方向键下弹出菜单条
                if (view.isControllerFullyVisible()) {
                    return super.onKeyDown(keyCode, event);
                }
                showMenuBar();
                return true;
            case KeyEvent.KEYCODE_DPAD_UP:
                // 方向键上：唤出原生控制器（播放/暂停/进度条）
                if (!view.isControllerFullyVisible()) {
                    view.showController();
                    return true;
                }
                return super.onKeyDown(keyCode, event);
            case KeyEvent.KEYCODE_DPAD_LEFT:
            case KeyEvent.KEYCODE_DPAD_RIGHT:
                // 控制条没亮：第一下左/右只唤出控制条，不跳转；
                // 控制条亮着：左右 = 快退/快进 10 秒并重置自动隐藏计时
                if (!view.isControllerFullyVisible()) {
                    view.showController();
                    return true;
                }
                if (keyCode == KeyEvent.KEYCODE_DPAD_LEFT) {
                    player.seekBack();
                } else {
                    player.seekForward();
                }
                view.showController();
                return true;
            default:
                return super.onKeyDown(keyCode, event);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        savePos();
        if (player != null) {
            player.pause();
        }
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (player != null) {
            player.release();
            player = null;
        }
        super.onDestroy();
    }
}

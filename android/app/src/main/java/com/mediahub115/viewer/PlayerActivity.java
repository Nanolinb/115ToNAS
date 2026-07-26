package com.mediahub115.viewer;

import android.app.Activity;
import android.net.Uri;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.WindowManager;
import android.widget.Toast;

import androidx.annotation.OptIn;
import androidx.media3.common.C;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MimeTypes;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.common.Tracks;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.ui.PlayerView;
import androidx.media3.ui.TrackSelectionDialogBuilder;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * 内嵌播放器：ExoPlayer 硬件解码（电视/投影 MediaCodec 可解 HEVC，
 * 浏览器 WebView 解不了的在这里也能播），应用内完成，不跳外部 App。
 *
 * - 外挂字幕：服务端 srt→vtt 直挂，ass 走 ExoPlayer 自带 SSA 解析
 * - 音轨菜单：遥控器 MENU 键；字幕菜单：CAPTIONS 键
 * - 音轨解码失败（无 DTS/TrueHD 授权）→ 自动回落服务端 ?audio=aac 转码流
 * - 遥控器左右键快退/快进 10 秒
 */
@OptIn(markerClass = UnstableApi.class)
public class PlayerActivity extends Activity {

    private ExoPlayer player;
    private PlayerView view;
    private String url;
    private String subsJson;
    private boolean aacFallback;  // 已切到服务端 AAC 转码流
    private boolean trackChecked; // 首次 onTracksChanged 已检测

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        url = getIntent().getStringExtra("url");
        subsJson = getIntent().getStringExtra("subs");
        String title = getIntent().getStringExtra("title");
        if (title != null && !title.isEmpty()) {
            setTitle(title);
        }

        view = new PlayerView(this);
        view.setUseController(true);
        view.setControllerShowTimeoutMs(5000);
        setContentView(view);

        player = new ExoPlayer.Builder(this).build();
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
        load(url, 0);
    }

    private void load(String u, long startMs) {
        MediaItem.Builder mb = new MediaItem.Builder().setUri(u);
        List<MediaItem.SubtitleConfiguration> cfgs = new ArrayList<>();
        try {
            JSONArray arr = new JSONArray(subsJson == null ? "[]" : subsJson);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                String ext = o.optString("ext", "vtt").toLowerCase();
                String mime = (ext.equals("ass") || ext.equals("ssa"))
                        ? MimeTypes.TEXT_SSA : MimeTypes.TEXT_VTT;
                cfgs.add(new MediaItem.SubtitleConfiguration.Builder(
                        Uri.parse(o.getString("url")))
                        .setMimeType(mime)
                        .setLabel(o.optString("label", "字幕 " + (i + 1)))
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

    private void switchToAac(String msg) {
        aacFallback = true;
        long posSec = Math.max(0, player.getCurrentPosition()) / 1000;
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
        String sep = url.contains("?") ? "&" : "?";
        load(url + sep + "audio=aac&t=" + posSec, posSec * 1000);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        switch (keyCode) {
            case KeyEvent.KEYCODE_MENU:
                new TrackSelectionDialogBuilder(this, "选择音轨", player,
                        C.TRACK_TYPE_AUDIO).build().show();
                return true;
            case KeyEvent.KEYCODE_CAPTIONS:
                new TrackSelectionDialogBuilder(this, "选择字幕", player,
                        C.TRACK_TYPE_TEXT).build().show();
                return true;
            case KeyEvent.KEYCODE_DPAD_LEFT:
                player.seekBack();
                return true;
            case KeyEvent.KEYCODE_DPAD_RIGHT:
                player.seekForward();
                return true;
            default:
                return super.onKeyDown(keyCode, event);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (player != null) {
            player.pause();
        }
    }

    @Override
    protected void onDestroy() {
        if (player != null) {
            player.release();
            player = null;
        }
        super.onDestroy();
    }
}

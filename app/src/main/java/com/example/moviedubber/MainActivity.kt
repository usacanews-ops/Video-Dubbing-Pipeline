package com.example.moviedubber

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.Buffer
import okio.BufferedSink
import okio.ForwardingSink
import okio.buffer
import okio.source
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit
import java.util.regex.Pattern

val PrimaryIndigo = Color(0xFF4F46E5)
val AccentCyan = Color(0xFF06B6D4)
val EmeraldSuccess = Color(0xFF10B981)
val CoralError = Color(0xFFEF4444)
val AmberWarning = Color(0xFFF59E0B)
val SurfaceCanvas = Color(0xFFF8FAFC)
val CardBackground = Color(0xFFFFFFFF)
val TextMuted = Color(0xFF64748B)

data class FacebookPageAccount(
    val name: String,
    val id: String,
    val token: String
)

data class QueueItem(
    val id: Long = System.currentTimeMillis(),
    val videoUrl: String,
    val targetLang: String = "hi",
    val speed: Float = 1.0f
)

data class HistoryItem(
    val id: String,
    var title: String,
    val downloadUrl: String,
    val srtUrl: String,
    val timestamp: String,
    var sourceMetaText: String = "",
    var isUploaded: Boolean = false,
    val deviceId: String = ""
)

class ProgressRequestBody(
    private val file: File,
    private val contentType: MediaType,
    private val onProgressUpdate: (percentage: Int, bytesWritten: Long, totalBytes: Long) -> Unit
) : RequestBody() {
    override fun contentType(): MediaType = contentType
    override fun contentLength(): Long = file.length()
    override fun writeTo(sink: BufferedSink) {
        val totalLength = contentLength()
        val countingSink = object : ForwardingSink(sink) {
            var bytesWritten = 0L
            override fun write(source: Buffer, byteCount: Long) {
                super.write(source, byteCount)
                bytesWritten += byteCount
                val progress = if (totalLength > 0) ((bytesWritten * 100) / totalLength).toInt() else 0
                onProgressUpdate(progress, bytesWritten, totalLength)
            }
        }
        val buffered = countingSink.buffer()
        file.source().use { source ->
            buffered.writeAll(source)
            buffered.flush()
        }
    }
}

class DubberForegroundService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val channelId = "dubber_background_channel"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                channelId,
                "Dubber Background Pipeline",
                NotificationManager.IMPORTANCE_LOW
            )
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
        val notification: Notification = NotificationCompat.Builder(this, channelId)
            .setContentTitle("MovieDubber Processing")
            .setContentText("Dubbing tasks active in background...")
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .build()

        startForeground(1001, notification)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY
}

object DubberQueueManager {
    val queue = ConcurrentLinkedQueue<QueueItem>()
    var isProcessing by mutableStateOf(false)
    var currentStatus by mutableStateOf("Ready to dub")
    var detailedLogs by mutableStateOf("")
    var firstLinePreview by mutableStateOf("")
    var queueSize by mutableStateOf(0)
    var historyList = mutableStateListOf<HistoryItem>()

    var activeRunId by mutableStateOf<Long?>(null)
    var canReconnect by mutableStateOf(false)
    var hasCloudError by mutableStateOf(false)

    private val coroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // Unique per-device identification
    fun getDeviceId(context: Context): String {
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        var devId = prefs.getString("device_id", null)
        if (devId.isNullOrBlank()) {
            devId = UUID.randomUUID().toString().replace("-", "").take(8)
            prefs.edit().putString("device_id", devId).apply()
        }
        return devId
    }

    // Rolling cap of max 25 deleted IDs
    fun getDeletedRunIds(context: Context): List<String> {
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val raw = prefs.getString("deleted_run_ids", "[]") ?: "[]"
        val list = mutableListOf<String>()
        try {
            val arr = JSONArray(raw)
            for (i in 0 until arr.length()) list.add(arr.getString(i))
        } catch (_: Exception) {}
        return list
    }

    fun markRunDeleted(context: Context, runId: String) {
        val current = getDeletedRunIds(context).toMutableList()
        if (!current.contains(runId)) {
            current.add(0, runId)
        }
        while (current.size > 25) {
            current.removeAt(current.size - 1)
        }
        val arr = JSONArray()
        current.forEach { arr.put(it) }
        context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
            .edit().putString("deleted_run_ids", arr.toString()).apply()
    }

    fun saveQueue(context: Context) {
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val arr = JSONArray()
        for (item in queue) {
            val obj = JSONObject().apply {
                put("id", item.id)
                put("videoUrl", item.videoUrl)
                put("targetLang", item.targetLang)
                put("speed", item.speed.toDouble())
            }
            arr.put(obj)
        }
        prefs.edit().putString("pending_queue_json", arr.toString()).apply()
    }

    fun loadQueue(context: Context) {
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val jsonStr = prefs.getString("pending_queue_json", "[]") ?: "[]"
        try {
            val arr = JSONArray(jsonStr)
            queue.clear()
            for (i in 0 until arr.length()) {
                val obj = arr.getJSONObject(i)
                queue.add(
                    QueueItem(
                        id = obj.getLong("id"),
                        videoUrl = obj.getString("videoUrl"),
                        targetLang = obj.optString("targetLang", "hi"),
                        speed = obj.optDouble("speed", 1.0).toFloat()
                    )
                )
            }
            queueSize = queue.size
        } catch (_: Exception) {}
    }

    fun loadHistory(context: Context) {
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val myDeviceId = getDeviceId(context)
        val jsonStr = prefs.getString("history_json", "[]") ?: "[]"
        try {
            val arr = JSONArray(jsonStr)
            historyList.clear()
            for (i in 0 until arr.length()) {
                val obj = arr.getJSONObject(i)
                val itemDevId = obj.optString("deviceId", myDeviceId)
                if (itemDevId == myDeviceId) {
                    historyList.add(
                        HistoryItem(
                            id = obj.getString("id"),
                            title = obj.optString("title", "Dubbed Video"),
                            downloadUrl = obj.getString("downloadUrl"),
                            srtUrl = obj.optString("srtUrl", ""),
                            timestamp = obj.getString("timestamp"),
                            sourceMetaText = obj.optString("sourceMetaText", ""),
                            isUploaded = obj.optBoolean("isUploaded", false),
                            deviceId = itemDevId
                        )
                    )
                }
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }

        loadQueue(context)

        // Perform bidirectional cloud reconciliation
        coroutineScope.launch {
            syncWithCloudOnStartup(context)
        }
    }

    fun saveHistory(context: Context) {
        val arr = JSONArray()
        for (h in historyList) {
            val obj = JSONObject().apply {
                put("id", h.id)
                put("title", h.title)
                put("downloadUrl", h.downloadUrl)
                put("srtUrl", h.srtUrl)
                put("timestamp", h.timestamp)
                put("sourceMetaText", h.sourceMetaText)
                put("isUploaded", h.isUploaded)
                put("deviceId", h.deviceId)
            }
            arr.put(obj)
        }
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        prefs.edit().putString("history_json", arr.toString()).apply()
    }

    fun removeItem(context: Context, item: HistoryItem) {
        markRunDeleted(context, item.id)
        historyList.remove(item)
        saveHistory(context)
    }

    fun clearAll(context: Context) {
        historyList.forEach { markRunDeleted(context, it.id) }
        historyList.clear()
        saveHistory(context)
    }

    fun addHistoryItem(context: Context, item: HistoryItem) {
        if (historyList.any { it.id == item.id }) return
        historyList.add(0, item)
        while (historyList.size > 25) {
            historyList.removeAt(historyList.size - 1)
        }
        saveHistory(context)
    }

    fun enqueue(item: QueueItem, context: Context) {
        queue.add(item)
        queueSize = queue.size
        saveQueue(context)
        processNext(context.applicationContext)
    }

    fun startService(context: Context) {
        try {
            val intent = Intent(context, DubberForegroundService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        } catch (_: Exception) {}
    }

    fun stopService(context: Context) {
        try {
            context.stopService(Intent(context, DubberForegroundService::class.java))
        } catch (_: Exception) {}
    }

    fun processNext(context: Context) {
        if (isProcessing || queue.isEmpty()) {
            if (queue.isEmpty() && !isProcessing) stopService(context)
            return
        }

        val nextItem = queue.poll() ?: return
        queueSize = queue.size
        saveQueue(context)

        isProcessing = true
        canReconnect = false
        hasCloudError = false
        startService(context)

        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val owner = prefs.getString("owner", "usacanews-ops") ?: ""
        val repo = prefs.getString("repo", "Video-Dubbing-Pipeline") ?: ""
        val token = prefs.getString("token", "") ?: ""
        val devId = getDeviceId(context)

        coroutineScope.launch {
            executeCloudDubbingPipeline(
                context = context,
                owner = owner.trim(),
                repo = repo.trim(),
                token = token.trim(),
                videoUrl = nextItem.videoUrl.trim(),
                targetLang = nextItem.targetLang,
                speed = nextItem.speed,
                deviceId = devId,
                onStatusUpdate = { status, log, preview ->
                    currentStatus = status
                    detailedLogs = log
                    if (preview.isNotBlank()) {
                        val words = preview.trim().split(Regex("\\s+"))
                        firstLinePreview = if (words.size > 5) words.take(5).joinToString(" ") + "..." else preview
                    }
                },
                onComplete = { _, _ ->
                    isProcessing = false
                    canReconnect = false
                    hasCloudError = false
                    activeRunId = null
                    prefs.edit().remove("last_active_run_id").apply()
                    currentStatus = "🎉 Dubbing complete! Ready to schedule."
                    processNext(context)
                },
                onError = { _ ->
                    isProcessing = false
                    canReconnect = (activeRunId != null)
                    hasCloudError = true
                    currentStatus = "Cloud Connectivity Error."
                    processNext(context)
                }
            )
        }
    }

    fun reconnect(context: Context) {
        val runId = activeRunId ?: return
        if (isProcessing) return
        isProcessing = true
        canReconnect = false
        hasCloudError = false
        currentStatus = "Re-establishing connection..."
        startService(context)

        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val owner = prefs.getString("owner", "usacanews-ops") ?: ""
        val repo = prefs.getString("repo", "Video-Dubbing-Pipeline") ?: ""
        val token = prefs.getString("token", "") ?: ""
        val devId = getDeviceId(context)

        coroutineScope.launch {
            monitorCloudRun(
                context = context,
                owner = owner.trim(),
                repo = repo.trim(),
                token = token.trim(),
                runId = runId,
                deviceId = devId,
                onStatusUpdate = { status, log, preview ->
                    currentStatus = status
                    detailedLogs = log
                    if (preview.isNotBlank()) {
                        val words = preview.trim().split(Regex("\\s+"))
                        firstLinePreview = if (words.size > 5) words.take(5).joinToString(" ") + "..." else preview
                    }
                },
                onComplete = { _, _ ->
                    isProcessing = false
                    canReconnect = false
                    hasCloudError = false
                    activeRunId = null
                    prefs.edit().remove("last_active_run_id").apply()
                    currentStatus = "🎉 Dubbing complete! Ready to schedule."
                    processNext(context)
                },
                onError = { _ ->
                    isProcessing = false
                    canReconnect = true
                    hasCloudError = true
                    currentStatus = "Cloud Connectivity Error."
                }
            )
        }
    }

    // Bidirectional reconciliation after deep sleep / app launch
    suspend fun syncWithCloudOnStartup(context: Context) = withContext(Dispatchers.IO) {
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val owner = prefs.getString("owner", "usacanews-ops")?.trim() ?: ""
        val repo = prefs.getString("repo", "Video-Dubbing-Pipeline")?.trim() ?: ""
        val token = prefs.getString("token", "")?.trim() ?: ""
        val myDeviceId = getDeviceId(context)

        if (owner.isBlank() || repo.isBlank() || token.isBlank()) return@withContext

        val client = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).build()
        val authHeader = "Bearer $token"
        val deletedIds = getDeletedRunIds(context)

        try {
            // 1. Recover completed releases generated while device was asleep
            val releasesUrl = "https://api.github.com/repos/$owner/$repo/releases?per_page=25"
            val relReq = Request.Builder().url(releasesUrl).addHeader("Authorization", authHeader).build()
            val relRes = client.newCall(relReq).execute()

            if (relRes.isSuccessful) {
                val relArray = JSONArray(relRes.body?.string() ?: "[]")
                for (i in 0 until relArray.length()) {
                    val rel = relArray.getJSONObject(i)
                    val tagName = rel.optString("tag_name", "")
                    
                    // Match releases created by this specific device
                    if (tagName.startsWith("release-$myDeviceId-")) {
                        val runIdStr = tagName.removePrefix("release-$myDeviceId-")
                        
                        // Ignore if deleted manually by user
                        if (deletedIds.contains(runIdStr)) continue
                        if (historyList.any { it.id == runIdStr }) continue

                        var videoUrl = ""
                        var srtUrl = ""
                        var sourceMeta = ""
                        var title = "Dubbed Video"

                        val assets = rel.optJSONArray("assets")
                        if (assets != null) {
                            for (j in 0 until assets.length()) {
                                val asset = assets.getJSONObject(j)
                                val name = asset.getString("name")
                                val downloadUrl = asset.getString("browser_download_url")

                                when {
                                    name.endsWith(".mp4") -> videoUrl = downloadUrl
                                    name.endsWith(".srt") -> srtUrl = downloadUrl
                                    name == "source_meta.json" -> {
                                        try {
                                            val mReq = Request.Builder().url(downloadUrl).addHeader("Authorization", authHeader).build()
                                            val mRes = client.newCall(mReq).execute()
                                            val parsed = JSONObject(mRes.body?.string() ?: "{}")
                                            title = parsed.optString("title", title)
                                            sourceMeta = "TITLE:\n${parsed.optString("title")}\n\nTAGS:\n${parsed.optJSONArray("tags")}\n\nDESCRIPTION:\n${parsed.optString("description")}"
                                        } catch (_: Exception) {}
                                    }
                                }
                            }
                        }

                        if (videoUrl.isNotBlank()) {
                            withContext(Dispatchers.Main) {
                                addHistoryItem(
                                    context,
                                    HistoryItem(
                                        id = runIdStr,
                                        title = title,
                                        downloadUrl = videoUrl,
                                        srtUrl = srtUrl,
                                        timestamp = "Recovered",
                                        sourceMetaText = sourceMeta,
                                        isUploaded = false,
                                        deviceId = myDeviceId
                                    )
                                )
                            }
                        }
                    }
                }
            }

            // 2. Check for an active running task initiated by this device
            val runsUrl = "https://api.github.com/repos/$owner/$repo/actions/runs?per_page=15"
            val runsReq = Request.Builder().url(runsUrl).addHeader("Authorization", authHeader).build()
            val runsRes = client.newCall(runsReq).execute()

            if (runsRes.isSuccessful) {
                val runsJson = JSONObject(runsRes.body?.string() ?: "{}")
                val runsArray = runsJson.optJSONArray("workflow_runs")
                if (runsArray != null) {
                    for (i in 0 until runsArray.length()) {
                        val run = runsArray.getJSONObject(i)
                        val runName = run.optString("name", "")
                        val status = run.optString("status", "")
                        val runId = run.getLong("id")

                        // Reattach to in_progress or queued runs belonging to this device
                        if (runName.contains("[$myDeviceId]") && (status == "in_progress" || status == "queued")) {
                            withContext(Dispatchers.Main) {
                                activeRunId = runId
                                isProcessing = true
                                canReconnect = false
                                hasCloudError = false
                                currentStatus = "⚙️ Re-attached to active run #$runId"
                                startService(context)
                                coroutineScope.launch {
                                    monitorCloudRun(
                                        context, owner, repo, token, runId, myDeviceId,
                                        onStatusUpdate = { s, l, p ->
                                            currentStatus = s
                                            detailedLogs = l
                                            if (p.isNotBlank()) {
                                                val words = p.trim().split(Regex("\\s+"))
                                                firstLinePreview = if (words.size > 5) words.take(5).joinToString(" ") + "..." else p
                                            }
                                        },
                                        onComplete = { _, _ ->
                                            isProcessing = false
                                            activeRunId = null
                                            processNext(context)
                                        },
                                        onError = { _ ->
                                            isProcessing = false
                                            canReconnect = true
                                            hasCloudError = true
                                            currentStatus = "Cloud Connectivity Error."
                                        }
                                    )
                                }
                            }
                            return@withContext
                        }
                    }
                }
            }

            // 3. If nothing is actively running on server, resume waiting local queue
            withContext(Dispatchers.Main) {
                if (!isProcessing && queue.isNotEmpty()) {
                    processNext(context)
                }
            }

        } catch (_: Exception) {}
    }
}

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        DubberQueueManager.loadHistory(this)
        handleIncomingShare(intent)

        setContent {
            MaterialTheme(
                colorScheme = lightColorScheme(
                    primary = PrimaryIndigo,
                    secondary = AccentCyan,
                    surface = CardBackground,
                    background = SurfaceCanvas
                )
            ) {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    DubberLiveApp()
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIncomingShare(intent)
    }

    private fun handleIncomingShare(intent: Intent?) {
        if (intent?.action == Intent.ACTION_SEND && intent.type == "text/plain") {
            val sharedText = intent.getStringExtra(Intent.EXTRA_TEXT) ?: ""
            val extractedUrl = extractUrl(sharedText)

            if (!extractedUrl.isNullOrBlank()) {
                val prefs = getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
                val lang = prefs.getString("default_lang", "hi") ?: "hi"
                val speed = prefs.getFloat("default_speed", 1.0f)

                DubberQueueManager.enqueue(
                    QueueItem(videoUrl = extractedUrl, targetLang = lang, speed = speed),
                    this
                )
                Toast.makeText(this, "📥 Added to processing queue", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun extractUrl(text: String): String? {
        val matcher = Pattern.compile("https?://[\\w\\-._~:/?#\\[\\]@!$&'()*+,;=%]+").matcher(text)
        return if (matcher.find()) matcher.group(0) else null
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DubberLiveApp() {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)

    var cloudWorkspace by remember { mutableStateOf(prefs.getString("owner", "usacanews-ops") ?: "") }
    var cloudRepository by remember { mutableStateOf(prefs.getString("repo", "Video-Dubbing-Pipeline") ?: "") }
    var cloudServerKey by remember { mutableStateOf(prefs.getString("token", "") ?: "") }

    var fbPagesRaw by remember {
        mutableStateOf(prefs.getString("fb_pages_raw", "Primary Page|100085938472910|EAA...") ?: "")
    }

    var customTitle by remember { mutableStateOf(prefs.getString("custom_title", "") ?: "") }
    var customTags by remember {
        mutableStateOf(prefs.getString("custom_tags", "#fyp #moviejet #reels #hindidubbed #movieexplained") ?: "")
    }

    var videoUrl by remember { mutableStateOf("") }
    var selectedLanguage by remember { mutableStateOf("hi") }
    var showSettings by remember { mutableStateOf(cloudServerKey.isBlank()) }

    var fbUploadStage by remember { mutableStateOf("") }
    var fbUploadPercent by remember { mutableIntStateOf(0) }
    var isUploadingToFb by remember { mutableStateOf(false) }

    var activeMetadataDialog by remember { mutableStateOf<String?>(null) }
    val deviceId = remember { DubberQueueManager.getDeviceId(context) }

    val pageAccounts = remember(fbPagesRaw) {
        fbPagesRaw.lines().mapNotNull { line ->
            val parts = line.split("|")
            if (parts.size >= 3) {
                FacebookPageAccount(parts[0].trim(), parts[1].trim(), parts[2].trim())
            } else null
        }
    }

    var selectedPageAccount by remember(pageAccounts) {
        mutableStateOf(pageAccounts.firstOrNull())
    }

    activeMetadataDialog?.let { metaText ->
        AlertDialog(
            onDismissRequest = { activeMetadataDialog = null },
            title = {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("ℹ️", fontSize = 18.sp)
                    Spacer(modifier = Modifier.width(6.dp))
                    Text("Source Video Metadata", fontSize = 16.sp, fontWeight = FontWeight.Bold)
                }
            },
            text = {
                Card(
                    modifier = Modifier.fillMaxWidth().height(260.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = CardDefaults.cardColors(containerColor = Color(0xFFF1F5F9))
                ) {
                    Box(
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(12.dp)
                            .verticalScroll(rememberScrollState())
                    ) {
                        SelectionContainer {
                            Text(
                                text = metaText,
                                fontSize = 12.sp,
                                lineHeight = 17.sp,
                                color = Color(0xFF334155),
                                fontFamily = FontFamily.Monospace
                            )
                        }
                    }
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                        clipboard.setPrimaryClip(ClipData.newPlainText("Source Metadata", metaText))
                        Toast.makeText(context, "📋 Metadata copied to clipboard", Toast.LENGTH_SHORT).show()
                        activeMetadataDialog = null
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo)
                ) {
                    Text("📋 Copy All")
                }
            },
            dismissButton = {
                TextButton(onClick = { activeMetadataDialog = null }) {
                    Text("Close", color = TextMuted)
                }
            }
        )
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(horizontal = 16.dp, vertical = 12.dp)
            .verticalScroll(rememberScrollState()),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 6.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column {
                Text(
                    "🎬 MovieDubber AI",
                    fontSize = 22.sp,
                    fontWeight = FontWeight.ExtraBold,
                    color = Color(0xFF0F172A)
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        modifier = Modifier
                            .size(7.dp)
                            .clip(CircleShape)
                            .background(if (DubberQueueManager.isProcessing) AmberWarning else EmeraldSuccess)
                    )
                    Spacer(modifier = Modifier.width(5.dp))
                    Text(
                        if (DubberQueueManager.isProcessing) "Engine Working" else "Ready",
                        fontSize = 11.sp,
                        fontWeight = FontWeight.Medium,
                        color = TextMuted
                    )
                    Text(
                        " • ID: $deviceId",
                        fontSize = 10.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = PrimaryIndigo
                    )
                    if (DubberQueueManager.queueSize > 0) {
                        Text(
                            " • Queue: ${DubberQueueManager.queueSize}",
                            fontSize = 11.sp,
                            fontWeight = FontWeight.SemiBold,
                            color = PrimaryIndigo
                        )
                    }
                }
            }

            FilledTonalButton(
                onClick = { showSettings = !showSettings },
                shape = RoundedCornerShape(10.dp),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp)
            ) {
                Text(if (showSettings) "✕ Close" else "⚙️ Settings", fontSize = 12.sp)
            }
        }

        AnimatedVisibility(visible = showSettings) {
            Card(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 8.dp),
                shape = RoundedCornerShape(14.dp),
                colors = CardDefaults.cardColors(containerColor = Color(0xFFF1F5F9))
            ) {
                Column(modifier = Modifier.padding(14.dp)) {
                    Text("Cloud Engine Configuration", fontWeight = FontWeight.Bold, fontSize = 13.sp, color = Color(0xFF1E293B))
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = cloudWorkspace,
                        onValueChange = { cloudWorkspace = it; prefs.edit().putString("owner", it).apply() },
                        label = { Text("Workspace / User") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp)
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = cloudRepository,
                        onValueChange = { cloudRepository = it; prefs.edit().putString("repo", it).apply() },
                        label = { Text("Pipeline Service Name") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp)
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = cloudServerKey,
                        onValueChange = { cloudServerKey = it; prefs.edit().putString("token", it).apply() },
                        label = { Text("Pipeline Access Token") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp)
                    )
                    Spacer(modifier = Modifier.height(10.dp))
                    Text("Facebook Pages (Name|PageID|Token)", fontWeight = FontWeight.Bold, fontSize = 13.sp, color = Color(0xFF1E293B))
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = fbPagesRaw,
                        onValueChange = {
                            fbPagesRaw = it
                            prefs.edit().putString("fb_pages_raw", it).apply()
                        },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 2,
                        shape = RoundedCornerShape(10.dp)
                    )
                }
            }
        }

        Spacer(modifier = Modifier.height(6.dp))

        OutlinedTextField(
            value = customTitle,
            onValueChange = {
                customTitle = it
                prefs.edit().putString("custom_title", it).apply()
            },
            label = { Text("Title (Tap below to fill)") },
            placeholder = { Text("Title (Tap below to fill)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            shape = RoundedCornerShape(12.dp),
            trailingIcon = {
                if (customTitle.isNotEmpty()) {
                    IconButton(
                        onClick = {
                            customTitle = ""
                            prefs.edit().putString("custom_title", "").apply()
                        }
                    ) {
                        Box(
                            modifier = Modifier
                                .size(20.dp)
                                .clip(CircleShape)
                                .background(Color(0xFFE2E8F0)),
                            contentAlignment = Alignment.Center
                        ) {
                            Text("✕", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color(0xFF475569))
                        }
                    }
                }
            }
        )

        Spacer(modifier = Modifier.height(6.dp))

        OutlinedTextField(
            value = customTags,
            onValueChange = {
                customTags = it
                prefs.edit().putString("custom_tags", it).apply()
            },
            label = { Text("Custom #Tags") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            shape = RoundedCornerShape(12.dp)
        )

        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = videoUrl,
            onValueChange = { videoUrl = it },
            label = { Text("Paste Video Link (FB / YouTube)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            shape = RoundedCornerShape(12.dp)
        )

        Spacer(modifier = Modifier.height(10.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Target Language", fontSize = 12.sp, fontWeight = FontWeight.SemiBold, color = TextMuted)
        }
        Spacer(modifier = Modifier.height(4.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            listOf(
                "hi" to "Hindi 🇮🇳",
                "en" to "English 🇺🇸",
                "es" to "Spanish 🇪🇸",
                "fr" to "French 🇫🇷",
                "de" to "German 🇩🇪",
                "it" to "Italian 🇮🇹",
                "pt" to "Portuguese 🇧🇷",
                "ja" to "Japanese 🇯🇵"
            ).forEach { (code, name) ->
                FilterChip(
                    selected = selectedLanguage == code,
                    onClick = {
                        selectedLanguage = code
                        prefs.edit().putString("default_lang", code).apply()
                    },
                    label = { Text(name, fontSize = 12.sp) },
                    shape = RoundedCornerShape(8.dp)
                )
            }
        }

        Spacer(modifier = Modifier.height(12.dp))

        Button(
            onClick = {
                if (videoUrl.isNotBlank()) {
                    DubberQueueManager.enqueue(
                        QueueItem(videoUrl = videoUrl.trim(), targetLang = selectedLanguage),
                        context
                    )
                    videoUrl = ""
                }
            },
            modifier = Modifier
                .fillMaxWidth()
                .height(48.dp),
            enabled = videoUrl.isNotBlank() && cloudServerKey.isNotBlank(),
            shape = RoundedCornerShape(12.dp),
            colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo)
        ) {
            Text("➕ Add to Dubbing Queue", fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
        }

        Spacer(modifier = Modifier.height(10.dp))

        if (isUploadingToFb) {
            Card(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 4.dp),
                shape = RoundedCornerShape(12.dp),
                colors = CardDefaults.cardColors(containerColor = Color(0xFFEFF6FF))
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text(fbUploadStage, fontWeight = FontWeight.Bold, fontSize = 12.sp, color = PrimaryIndigo)
                        Text("$fbUploadPercent%", fontWeight = FontWeight.Bold, fontSize = 12.sp, color = PrimaryIndigo)
                    }
                    Spacer(modifier = Modifier.height(6.dp))
                    LinearProgressIndicator(
                        progress = fbUploadPercent / 100f,
                        modifier = Modifier.fillMaxWidth().height(6.dp).clip(RoundedCornerShape(3.dp)),
                        color = PrimaryIndigo,
                        trackColor = Color(0xFFDBEAFE)
                    )
                }
            }
        }

        Card(
            modifier = Modifier
                .fillMaxWidth()
                .border(
                    width = 1.dp,
                    color = if (DubberQueueManager.hasCloudError) CoralError.copy(alpha = 0.4f) else Color(0xFFE2E8F0),
                    shape = RoundedCornerShape(14.dp)
                ),
            shape = RoundedCornerShape(14.dp),
            colors = CardDefaults.cardColors(
                containerColor = if (DubberQueueManager.hasCloudError) Color(0xFFFEF2F2) else CardBackground
            )
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = DubberQueueManager.currentStatus,
                        fontWeight = FontWeight.Bold,
                        fontSize = 13.sp,
                        color = if (DubberQueueManager.hasCloudError) CoralError else Color(0xFF0F172A)
                    )

                    if (DubberQueueManager.isProcessing) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(16.dp),
                            strokeWidth = 2.dp,
                            color = PrimaryIndigo
                        )
                    }
                }

                if (DubberQueueManager.firstLinePreview.isNotBlank()) {
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        "🗣️ 1st Line: \"${DubberQueueManager.firstLinePreview}\"",
                        fontSize = 12.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = Color(0xFF0D9488)
                    )
                }

                if (DubberQueueManager.detailedLogs.isNotBlank()) {
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        DubberQueueManager.detailedLogs,
                        fontSize = 11.sp,
                        color = TextMuted,
                        fontFamily = FontFamily.Monospace
                    )
                }

                if (DubberQueueManager.canReconnect && DubberQueueManager.activeRunId != null) {
                    Spacer(modifier = Modifier.height(10.dp))
                    Button(
                        onClick = { DubberQueueManager.reconnect(context) },
                        colors = ButtonDefaults.buttonColors(containerColor = CoralError),
                        shape = RoundedCornerShape(10.dp),
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(40.dp)
                    ) {
                        Text(
                            "🔄 Retry",
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Bold,
                            color = Color.White
                        )
                    }
                }
            }
        }

        if (DubberQueueManager.historyList.isNotEmpty()) {
            Spacer(modifier = Modifier.height(16.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    "🕒 Dubbed Videos (${DubberQueueManager.historyList.size})",
                    fontWeight = FontWeight.Bold,
                    fontSize = 14.sp,
                    color = Color(0xFF0F172A)
                )
                TextButton(
                    onClick = { DubberQueueManager.clearAll(context) },
                    colors = ButtonDefaults.textButtonColors(contentColor = CoralError)
                ) {
                    Text("🗑️ Clear List", fontSize = 12.sp)
                }
            }
            Spacer(modifier = Modifier.height(4.dp))

            Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                DubberQueueManager.historyList.forEach { history ->
                    var cardScheduleMinutes by remember { mutableStateOf("0") }

                    Card(
                        modifier = Modifier
                            .fillMaxWidth()
                            .border(1.dp, Color(0xFFE2E8F0), RoundedCornerShape(14.dp)),
                        shape = RoundedCornerShape(14.dp),
                        colors = CardDefaults.cardColors(containerColor = CardBackground)
                    ) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Column(modifier = Modifier.weight(1f)) {
                                    Text(
                                        text = history.title,
                                        fontWeight = FontWeight.Bold,
                                        fontSize = 13.sp,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                        color = Color(0xFF0F172A),
                                        modifier = Modifier.clickable {
                                            customTitle = history.title
                                            prefs.edit().putString("custom_title", history.title).apply()
                                            Toast.makeText(context, "Populated title in Custom Box!", Toast.LENGTH_SHORT).show()
                                        }
                                    )
                                    Text(history.timestamp, fontSize = 10.sp, color = TextMuted)
                                }

                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    if (history.sourceMetaText.isNotBlank()) {
                                        IconButton(
                                            onClick = { activeMetadataDialog = history.sourceMetaText },
                                            modifier = Modifier.size(28.dp)
                                        ) {
                                            Text("ℹ️", fontSize = 13.sp)
                                        }
                                    }

                                    Spacer(modifier = Modifier.width(4.dp))

                                    IconButton(
                                        onClick = { DubberQueueManager.removeItem(context, history) },
                                        modifier = Modifier.size(28.dp)
                                    ) {
                                        Text("✕", fontWeight = FontWeight.Bold, color = TextMuted, fontSize = 13.sp)
                                    }
                                }
                            }

                            Spacer(modifier = Modifier.height(8.dp))

                            var dropdownExpanded by remember { mutableStateOf(false) }
                            Box(modifier = Modifier.fillMaxWidth()) {
                                OutlinedButton(
                                    onClick = { dropdownExpanded = true },
                                    modifier = Modifier.fillMaxWidth().height(36.dp),
                                    shape = RoundedCornerShape(8.dp),
                                    contentPadding = PaddingValues(horizontal = 10.dp)
                                ) {
                                    Text(
                                        "Target: ${selectedPageAccount?.name ?: "Select Target Page"} ▼",
                                        fontSize = 11.sp,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis
                                    )
                                }
                                DropdownMenu(
                                    expanded = dropdownExpanded,
                                    onDismissRequest = { dropdownExpanded = false }
                                ) {
                                    pageAccounts.forEach { acc ->
                                        DropdownMenuItem(
                                            text = { Text("${acc.name} (${acc.id})") },
                                            onClick = {
                                                selectedPageAccount = acc
                                                dropdownExpanded = false
                                            }
                                        )
                                    }
                                }
                            }

                            Spacer(modifier = Modifier.height(8.dp))

                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                Text("🕒 Schedule:", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = TextMuted)
                                OutlinedTextField(
                                    value = cardScheduleMinutes,
                                    onValueChange = { cardScheduleMinutes = it.filter { ch -> ch.isDigit() } },
                                    placeholder = { Text("0=Now", fontSize = 10.sp) },
                                    modifier = Modifier.width(90.dp).height(42.dp),
                                    shape = RoundedCornerShape(8.dp),
                                    textStyle = androidx.compose.ui.text.TextStyle(fontSize = 11.sp),
                                    singleLine = true
                                )
                                Text(
                                    text = if ((cardScheduleMinutes.toLongOrNull() ?: 0L) >= 20L) "Will schedule" else "Immediate",
                                    fontSize = 10.sp,
                                    color = if ((cardScheduleMinutes.toLongOrNull() ?: 0L) >= 20L) PrimaryIndigo else TextMuted
                                )
                            }

                            Spacer(modifier = Modifier.height(10.dp))

                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(6.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Button(
                                    onClick = {
                                        try {
                                            val streamIntent = Intent(Intent.ACTION_VIEW).apply {
                                                setDataAndType(Uri.parse(history.downloadUrl), "video/*")
                                                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                                            }
                                            context.startActivity(Intent.createChooser(streamIntent, "Watch Video"))
                                        } catch (e: Exception) {
                                            Toast.makeText(context, "No video player installed", Toast.LENGTH_SHORT).show()
                                        }
                                    },
                                    modifier = Modifier.size(38.dp),
                                    shape = CircleShape,
                                    colors = ButtonDefaults.buttonColors(containerColor = EmeraldSuccess),
                                    contentPadding = PaddingValues(0.dp)
                                ) {
                                    Text("▶", fontSize = 13.sp, color = Color.White)
                                }

                                OutlinedButton(
                                    onClick = {
                                        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                                        clipboard.setPrimaryClip(ClipData.newPlainText("Link", history.downloadUrl))
                                        Toast.makeText(context, "📋 Link copied!", Toast.LENGTH_SHORT).show()
                                    },
                                    modifier = Modifier.size(38.dp),
                                    shape = CircleShape,
                                    contentPadding = PaddingValues(0.dp)
                                ) {
                                    Text("📋", fontSize = 12.sp)
                                }

                                OutlinedButton(
                                    onClick = {
                                        val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse(history.downloadUrl))
                                        context.startActivity(browserIntent)
                                    },
                                    modifier = Modifier.size(38.dp),
                                    shape = CircleShape,
                                    contentPadding = PaddingValues(0.dp)
                                ) {
                                    Text("↗", fontSize = 13.sp, fontWeight = FontWeight.Bold)
                                }

                                val schedMins = cardScheduleMinutes.toLongOrNull() ?: 0L
                                val isScheduledMode = schedMins >= 20L

                                Button(
                                    onClick = {
                                        val targetPage = selectedPageAccount
                                        if (targetPage == null) {
                                            Toast.makeText(context, "Configure at least one Page in Config!", Toast.LENGTH_SHORT).show()
                                            showSettings = true
                                        } else {
                                            isUploadingToFb = true
                                            val finalTitle = customTitle.ifBlank { history.title }
                                            val finalTags = customTags.ifBlank { "#fyp #moviejet #reels #hindidubbed #movieexplained" }
                                            val finalCaption = "$finalTitle\n.\n.\n$finalTags"

                                            val scheduleUnix = if (isScheduledMode) {
                                                (System.currentTimeMillis() / 1000) + (schedMins * 60)
                                            } else null

                                            coroutineScope.launch {
                                                uploadUrlDirectlyToFacebook(
                                                    context = context,
                                                    pageId = targetPage.id,
                                                    pageToken = targetPage.token,
                                                    videoUrl = history.downloadUrl,
                                                    srtUrl = history.srtUrl,
                                                    description = finalCaption,
                                                    scheduledPublishTime = scheduleUnix,
                                                    onProgress = { stage, percent ->
                                                        fbUploadStage = stage
                                                        fbUploadPercent = percent
                                                    },
                                                    onSuccess = {
                                                        isUploadingToFb = false
                                                        history.isUploaded = true
                                                        DubberQueueManager.saveHistory(context)

                                                        customTitle = ""
                                                        prefs.edit().putString("custom_title", "").apply()

                                                        val msg = if (scheduleUnix != null) "📅 Scheduled successfully for +$schedMins mins!" else "🎉 Published directly to FB!"
                                                        Toast.makeText(context, msg, Toast.LENGTH_LONG).show()
                                                    },
                                                    onError = { err ->
                                                        isUploadingToFb = false
                                                        Toast.makeText(context, "❌ Upload Error: $err", Toast.LENGTH_LONG).show()
                                                    }
                                                )
                                            }
                                        }
                                    },
                                    modifier = Modifier.weight(1f).height(38.dp),
                                    shape = RoundedCornerShape(10.dp),
                                    colors = ButtonDefaults.buttonColors(
                                        containerColor = when {
                                            isScheduledMode -> PrimaryIndigo
                                            history.isUploaded -> AmberWarning
                                            else -> Color(0xFF1877F2)
                                        }
                                    ),
                                    contentPadding = PaddingValues(horizontal = 6.dp)
                                ) {
                                    val btnLabel = when {
                                        isScheduledMode -> "📅 Schedule"
                                        history.isUploaded -> "🔄 Re-upload"
                                        else -> "🚀 Upload FB"
                                    }
                                    Text(btnLabel, fontSize = 11.sp, color = Color.White, fontWeight = FontWeight.SemiBold)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

// =========================================================================
// 🚀 Cloud Pipeline Orchestrator with Persistent Monitoring & Resumption
// =========================================================================
suspend fun executeCloudDubbingPipeline(
    context: Context,
    owner: String,
    repo: String,
    token: String,
    videoUrl: String,
    targetLang: String,
    speed: Float,
    deviceId: String,
    onStatusUpdate: (String, String, String) -> Unit,
    onComplete: (String, String) -> Unit,
    onError: (String) -> Unit
) = withContext(Dispatchers.IO) {
    val client = OkHttpClient()
    val authHeader = "Bearer $token"
    val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)

    try {
        onStatusUpdate("⚡ Dispatching task...", "Connecting to cloud worker pipeline...", "")

        val dispatchUrl = "https://api.github.com/repos/$owner/$repo/actions/workflows/dub_video.yml/dispatches"
        val payload = JSONObject().apply {
            put("ref", "main")
            put("inputs", JSONObject().apply {
                put("video_url", videoUrl)
                put("target_language", targetLang)
                put("video_speed", speed.toString())
                put("device_id", deviceId)
            })
        }

        val request = Request.Builder()
            .url(dispatchUrl)
            .addHeader("Authorization", authHeader)
            .addHeader("Accept", "application/vnd.github+json")
            .post(payload.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val response = client.newCall(request).execute()
        if (!response.isSuccessful && response.code != 204) {
            onError("HTTP ${response.code}: Check Pipeline Credentials")
            return@withContext
        }

        onStatusUpdate("⏳ Queued on cloud...", "Awaiting available worker instance...", "")
        delay(6000)

        var runId: Long? = null
        for (i in 1..12) {
            val runsUrl = "https://api.github.com/repos/$owner/$repo/actions/runs?event=workflow_dispatch&per_page=10"
            val runsReq = Request.Builder().url(runsUrl).addHeader("Authorization", authHeader).build()
            val runsRes = client.newCall(runsReq).execute()
            if (runsRes.isSuccessful) {
                val json = JSONObject(runsRes.body?.string() ?: "")
                val runsArray = json.optJSONArray("workflow_runs")
                if (runsArray != null) {
                    for (k in 0 until runsArray.length()) {
                        val r = runsArray.getJSONObject(k)
                        val name = r.optString("name", "")
                        if (name.contains("[$deviceId]")) {
                            runId = r.getLong("id")
                            break
                        }
                    }
                    if (runId != null) break
                }
            }
            delay(3000)
        }

        if (runId == null) {
            onError("Task could not be initialized on the server.")
            return@withContext
        }

        DubberQueueManager.activeRunId = runId
        prefs.edit().putLong("last_active_run_id", runId).apply()

        monitorCloudRun(context, owner, repo, token, runId, deviceId, onStatusUpdate, onComplete, onError)

    } catch (e: Exception) {
        withContext(Dispatchers.Main) {
            onError(e.localizedMessage ?: "Processing error")
        }
    }
}

suspend fun monitorCloudRun(
    context: Context,
    owner: String,
    repo: String,
    token: String,
    runId: Long,
    deviceId: String,
    onStatusUpdate: (String, String, String) -> Unit,
    onComplete: (String, String) -> Unit,
    onError: (String) -> Unit
) = withContext(Dispatchers.IO) {
    val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()
    val authHeader = "Bearer $token"

    var isDone = false
    var runConclusion = ""
    var extractedPreview = ""
    var emittedTitle = "Dubbed Video"
    var consecutiveNetworkErrors = 0

    while (!isDone) {
        delay(3500)
        try {
            val jobUrl = "https://api.github.com/repos/$owner/$repo/actions/runs/$runId/jobs"
            val jobReq = Request.Builder().url(jobUrl).addHeader("Authorization", authHeader).build()
            val jobRes = client.newCall(jobReq).execute()

            if (jobRes.isSuccessful) {
                consecutiveNetworkErrors = 0
                val jobJson = JSONObject(jobRes.body?.string() ?: "")
                val jobs = jobJson.optJSONArray("jobs")
                if (jobs != null && jobs.length() > 0) {
                    val job = jobs.getJSONObject(0)
                    val jobId = job.optLong("id")
                    val status = job.optString("status")
                    val conclusion = job.optString("conclusion")
                    val steps = job.optJSONArray("steps")

                    var activeStep = "Processing audio/video streams..."
                    if (steps != null) {
                        for (j in 0 until steps.length()) {
                            val step = steps.getJSONObject(j)
                            if (step.optString("status") == "in_progress") {
                                activeStep = step.optString("name")
                                break
                            }
                        }
                    }

                    if (jobId != 0L) {
                        try {
                            val logReq = Request.Builder()
                                .url("https://api.github.com/repos/$owner/$repo/actions/jobs/$jobId/logs")
                                .addHeader("Authorization", authHeader)
                                .build()
                            val logRes = client.newCall(logReq).execute()
                            if (logRes.isSuccessful) {
                                val logText = logRes.body?.string() ?: ""

                                if (extractedPreview.isBlank()) {
                                    val m = Pattern.compile("TRANSLATION_PREVIEW:\\s*(.+)").matcher(logText)
                                    if (m.find()) extractedPreview = m.group(1)?.trim() ?: ""
                                }

                                val tm = Pattern.compile("TITLE_EMIT:\\s*(.+)").matcher(logText)
                                if (tm.find()) emittedTitle = tm.group(1)?.trim() ?: emittedTitle
                            }
                        } catch (_: Exception) {}
                    }

                    onStatusUpdate("⚙️ $activeStep", "Task #$runId", extractedPreview)

                    if (status == "completed") {
                        isDone = true
                        runConclusion = conclusion
                    }
                }
            } else {
                consecutiveNetworkErrors++
                if (consecutiveNetworkErrors > 6) {
                    withContext(Dispatchers.Main) { onError("Cloud Connectivity Error.") }
                    return@withContext
                }
            }
        } catch (e: Exception) {
            consecutiveNetworkErrors++
            if (consecutiveNetworkErrors <= 6) {
                onStatusUpdate("📡 Signal weak. Reconnecting ($consecutiveNetworkErrors/6)...", "Network timeout, retrying...", "")
                delay(3000)
                continue
            } else {
                withContext(Dispatchers.Main) {
                    onError("Cloud Connectivity Error.")
                }
                return@withContext
            }
        }
    }

    if (runConclusion != "success") {
        withContext(Dispatchers.Main) { onError("Task completed with status: $runConclusion") }
        return@withContext
    }

    onStatusUpdate("🔗 Fetching cloud assets...", "Assembling media package...", extractedPreview)
    delay(2000)

    val releaseTagUrl = "https://api.github.com/repos/$owner/$repo/releases/tags/release-$deviceId-$runId"
    val relReq = Request.Builder().url(releaseTagUrl).addHeader("Authorization", authHeader).build()
    val relRes = client.newCall(relReq).execute()

    var videoDownloadUrl = ""
    var srtDownloadUrl = ""
    var sourceMetadataJson = ""

    if (relRes.isSuccessful) {
        val relJson = JSONObject(relRes.body?.string() ?: "")
        val assets = relJson.optJSONArray("assets")
        if (assets != null) {
            for (k in 0 until assets.length()) {
                val asset = assets.getJSONObject(k)
                val name = asset.getString("name")
                if (name.endsWith(".mp4") || name == "final_output.mp4") {
                    videoDownloadUrl = asset.getString("browser_download_url")
                } else if (name.endsWith(".srt") || name == "subtitles.srt") {
                    srtDownloadUrl = asset.getString("browser_download_url")
                } else if (name == "source_meta.json") {
                    try {
                        val mfReq = Request.Builder().url(asset.getString("browser_download_url")).addHeader("Authorization", authHeader).build()
                        val mfRes = client.newCall(mfReq).execute()
                        val mfStr = mfRes.body?.string() ?: ""
                        val parsed = JSONObject(mfStr)
                        emittedTitle = parsed.optString("title", emittedTitle)
                        sourceMetadataJson = "TITLE:\n${parsed.optString("title")}\n\nTAGS:\n${parsed.optJSONArray("tags")}\n\nDESCRIPTION:\n${parsed.optString("description")}"
                    } catch (_: Exception) {}
                }
            }
        }
    }

    if (videoDownloadUrl.isBlank()) {
        videoDownloadUrl = "https://github.com/$owner/$repo/actions/runs/$runId"
    }

    val timeStr = SimpleDateFormat("MMM dd, HH:mm", Locale.getDefault()).format(Date())
    withContext(Dispatchers.Main) {
        DubberQueueManager.addHistoryItem(
            context,
            HistoryItem(
                id = runId.toString(),
                title = emittedTitle,
                downloadUrl = videoDownloadUrl,
                srtUrl = srtDownloadUrl,
                timestamp = timeStr,
                sourceMetaText = sourceMetadataJson,
                isUploaded = false,
                deviceId = deviceId
            )
        )
        onComplete(videoDownloadUrl, srtDownloadUrl)
    }
}

// =========================================================================
// 🎬 Facebook Reels Publisher (Live Buffering + Live Socket Upload Tracking)
// =========================================================================
suspend fun uploadUrlDirectlyToFacebook(
    context: Context,
    pageId: String,
    pageToken: String,
    videoUrl: String,
    srtUrl: String,
    description: String,
    scheduledPublishTime: Long?,
    onProgress: (String, Int) -> Unit,
    onSuccess: () -> Unit,
    onError: (String) -> Unit
) = withContext(Dispatchers.IO) {
    val uploadClient = OkHttpClient.Builder()
        .connectTimeout(180, TimeUnit.SECONDS)
        .readTimeout(300, TimeUnit.SECONDS)
        .writeTimeout(300, TimeUnit.SECONDS)
        .build()

    val tempVideoFile = File(context.cacheDir, "fb_video_${System.currentTimeMillis()}.mp4")
    val tempSrtFile = File(context.cacheDir, "fb_captions_${System.currentTimeMillis()}.srt")

    try {
        onProgress("Initializing session on Meta...", 0)

        val initUrl = "https://graph.facebook.com/v20.0/$pageId/video_reels"
        val initPayload = JSONObject().apply {
            put("upload_phase", "start")
            put("access_token", pageToken)
        }
        val initReq = Request.Builder()
            .url(initUrl)
            .post(initPayload.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val initRes = uploadClient.newCall(initReq).execute()
        val initJson = JSONObject(initRes.body?.string() ?: "")
        val videoId = initJson.optString("video_id")
        val uploadUrl = initJson.optString("upload_url")

        if (videoId.isBlank() || uploadUrl.isBlank()) {
            val errorMsg = initJson.optJSONObject("error")?.optString("message") ?: "Session init failed"
            withContext(Dispatchers.Main) { onError(errorMsg) }
            return@withContext
        }

        val sourceReq = Request.Builder().url(videoUrl).build()
        val sourceRes = uploadClient.newCall(sourceReq).execute()

        if (!sourceRes.isSuccessful || sourceRes.body == null) {
            withContext(Dispatchers.Main) { onError("Could not download video: HTTP ${sourceRes.code}") }
            return@withContext
        }

        val totalContentLength = sourceRes.body!!.contentLength()
        val inputStream: InputStream = sourceRes.body!!.byteStream()

        FileOutputStream(tempVideoFile).use { output ->
            val buffer = ByteArray(8192)
            var bytesRead: Int
            var totalBytesRead = 0L

            while (inputStream.read(buffer).also { bytesRead = it } != -1) {
                output.write(buffer, 0, bytesRead)
                totalBytesRead += bytesRead

                if (totalContentLength > 0) {
                    val percent = ((totalBytesRead * 100) / totalContentLength).toInt()
                    val mbRead = totalBytesRead / (1024 * 1024)
                    val mbTotal = totalContentLength / (1024 * 1024)
                    onProgress("Buffering from server: $percent% ($mbRead MB / $mbTotal MB)", percent)
                } else {
                    val mbRead = totalBytesRead / (1024 * 1024)
                    onProgress("Buffering from server: $mbRead MB", 50)
                }
            }
            output.flush()
        }

        val countingBody = ProgressRequestBody(
            file = tempVideoFile,
            contentType = "application/octet-stream".toMediaType()
        ) { percent, sent, total ->
            val mbSent = sent / (1024 * 1024)
            val mbTotal = total / (1024 * 1024)
            onProgress("Uploading to FB: $percent% ($mbSent MB / $mbTotal MB)", percent)
        }

        val uploadReq = Request.Builder()
            .url(uploadUrl)
            .addHeader("Authorization", "OAuth $pageToken")
            .addHeader("offset", "0")
            .addHeader("file_size", tempVideoFile.length().toString())
            .post(countingBody)
            .build()

        uploadClient.newCall(uploadReq).execute().close()

        if (srtUrl.isNotBlank()) {
            try {
                onProgress("Syncing subtitles...", 95)
                val srtReq = Request.Builder().url(srtUrl).build()
                val srtRes = uploadClient.newCall(srtReq).execute()
                if (srtRes.isSuccessful && srtRes.body != null) {
                    srtRes.body!!.byteStream().use { input ->
                        FileOutputStream(tempSrtFile).use { output -> input.copyTo(output) }
                    }

                    val captionBody = MultipartBody.Builder()
                        .setType(MultipartBody.FORM)
                        .addFormDataPart("access_token", pageToken)
                        .addFormDataPart(
                            "captions_file",
                            "captions.srt",
                            tempSrtFile.asRequestBody("application/x-subrip".toMediaType())
                        )
                        .build()

                    val captionReq = Request.Builder()
                        .url("https://graph.facebook.com/v20.0/$videoId/captions")
                        .post(captionBody)
                        .build()

                    uploadClient.newCall(captionReq).execute().close()
                }
            } catch (_: Exception) {}
        }

        onProgress(if (scheduledPublishTime != null) "Scheduling Reel on Meta..." else "Publishing Reel...", 99)
        val publishUrl = "https://graph.facebook.com/v20.0/$pageId/video_reels"
        val pubPayload = JSONObject().apply {
            put("upload_phase", "finish")
            put("access_token", pageToken)
            put("video_id", videoId)
            put("description", description)

            if (scheduledPublishTime != null) {
                put("video_state", "SCHEDULED")
                put("scheduled_publish_time", scheduledPublishTime)
            } else {
                put("video_state", "PUBLISHED")
            }

            put("privacy", JSONObject().apply {
                put("value", "EVERYONE")
            })
        }

        val pubReq = Request.Builder()
            .url(publishUrl)
            .post(pubPayload.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val pubRes = uploadClient.newCall(pubReq).execute()
        val pubJson = JSONObject(pubRes.body?.string() ?: "")

        if (pubJson.optBoolean("success", false) || pubJson.has("video_id")) {
            withContext(Dispatchers.Main) { onSuccess() }
        } else {
            val errorMsg = pubJson.optJSONObject("error")?.optString("message") ?: "Dispatch failed"
            withContext(Dispatchers.Main) { onError(errorMsg) }
        }

    } catch (e: Exception) {
        withContext(Dispatchers.Main) { onError(e.localizedMessage ?: "Network error during upload") }
    } finally {
        if (tempVideoFile.exists()) tempVideoFile.delete()
        if (tempSrtFile.exists()) tempSrtFile.delete()
    }
}

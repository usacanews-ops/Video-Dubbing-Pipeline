package com.example.moviedubber

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
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
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit
import java.util.regex.Pattern

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
    val title: String,
    val downloadUrl: String,
    val srtUrl: String,
    val timestamp: String,
    var isUploaded: Boolean = false
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
        val bufferedCountingSink = countingSink.buffer()
        file.source().use { source ->
            bufferedCountingSink.writeAll(source)
            bufferedCountingSink.flush()
        }
    }
}

object DubberQueueManager {
    val queue = ConcurrentLinkedQueue<QueueItem>()
    var isProcessing by mutableStateOf(false)
    var currentStatus by mutableStateOf("Ready to dub")
    var detailedLogs by mutableStateOf("")
    var firstLinePreview by mutableStateOf("")
    var queueSize by mutableStateOf(0)
    var historyList = mutableStateListOf<HistoryItem>()

    private val coroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    fun loadHistory(context: Context) {
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val jsonStr = prefs.getString("history_json", "[]") ?: "[]"
        try {
            val arr = JSONArray(jsonStr)
            historyList.clear()
            for (i in 0 until arr.length()) {
                val obj = arr.getJSONObject(i)
                historyList.add(
                    HistoryItem(
                        id = obj.getString("id"),
                        title = obj.getString("title"),
                        downloadUrl = obj.getString("downloadUrl"),
                        srtUrl = obj.optString("srtUrl", ""),
                        timestamp = obj.getString("timestamp"),
                        isUploaded = obj.optBoolean("isUploaded", false)
                    )
                )
            }
        } catch (e: Exception) {
            e.printStackTrace()
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
                put("isUploaded", h.isUploaded)
            }
            arr.put(obj)
        }
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        prefs.edit().putString("history_json", arr.toString()).apply()
    }

    fun addHistoryItem(context: Context, item: HistoryItem) {
        historyList.add(0, item)
        while (historyList.size > 10) {
            historyList.removeAt(historyList.size - 1)
        }
        saveHistory(context)
    }

    fun enqueue(item: QueueItem, context: Context) {
        queue.add(item)
        queueSize = queue.size
        processNext(context.applicationContext)
    }

    fun processNext(context: Context) {
        if (isProcessing || queue.isEmpty()) return

        val nextItem = queue.poll() ?: return
        queueSize = queue.size
        isProcessing = true

        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val owner = prefs.getString("owner", "usacanews-ops") ?: ""
        val repo = prefs.getString("repo", "Video-Dubbing-Pipeline") ?: ""
        val token = prefs.getString("token", "") ?: ""

        coroutineScope.launch {
            executeCloudDubbingPipeline(
                context = context,
                owner = owner.trim(),
                repo = repo.trim(),
                token = token.trim(),
                videoUrl = nextItem.videoUrl.trim(),
                targetLang = nextItem.targetLang,
                speed = nextItem.speed,
                onStatusUpdate = { status, log, preview ->
                    currentStatus = status
                    detailedLogs = log
                    if (preview.isNotBlank()) firstLinePreview = preview
                },
                onComplete = { _, _ ->
                    isProcessing = false
                    currentStatus = "🎉 Dub Finished! Link ready."
                    processNext(context)
                },
                onError = { err ->
                    isProcessing = false
                    currentStatus = "❌ Error: $err"
                    processNext(context)
                }
            )
        }
    }
}

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        DubberQueueManager.loadHistory(this)
        handleIncomingShare(intent)

        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
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
                Toast.makeText(this, "📥 Added to Dubbing Queue (#${DubberQueueManager.queueSize})", Toast.LENGTH_SHORT).show()
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

    var githubOwner by remember { mutableStateOf(prefs.getString("owner", "usacanews-ops") ?: "") }
    var githubRepo by remember { mutableStateOf(prefs.getString("repo", "Video-Dubbing-Pipeline") ?: "") }
    var githubToken by remember { mutableStateOf(prefs.getString("token", "") ?: "") }

    var fbPagesRaw by remember {
        mutableStateOf(prefs.getString("fb_pages_raw", "Main Page|100085938472910|EAA...") ?: "")
    }

    var customTitle by remember { mutableStateOf(prefs.getString("custom_title", "") ?: "") }
    var customTags by remember {
        mutableStateOf(prefs.getString("custom_tags", "#fyp #moviejet #reels #hindidubbed #movieexplained") ?: "")
    }

    var videoUrl by remember { mutableStateOf("") }
    var selectedLanguage by remember { mutableStateOf("hi") }
    var showSettings by remember { mutableStateOf(githubToken.isBlank()) }

    var fbUploadStage by remember { mutableStateOf("") }
    var fbUploadPercent by remember { mutableIntStateOf(0) }
    var isUploadingToFb by remember { mutableStateOf(false) }

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

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
            .verticalScroll(rememberScrollState()),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column {
                Text("🎬 AI Movie Dubber", fontSize = 22.sp, fontWeight = FontWeight.Bold)
                if (DubberQueueManager.queueSize > 0 || DubberQueueManager.isProcessing) {
                    Text(
                        "Queue: ${DubberQueueManager.queueSize} waiting | Processing: ${if (DubberQueueManager.isProcessing) "1" else "0"}",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.primary,
                        fontWeight = FontWeight.SemiBold
                    )
                }
            }
            TextButton(onClick = { showSettings = !showSettings }) {
                Text(if (showSettings) "Hide Config" else "⚙️ Config")
            }
        }

        if (showSettings) {
            Card(
                modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
            ) {
                Column(modifier = Modifier.padding(14.dp)) {
                    Text("GitHub Settings", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = githubOwner,
                        onValueChange = { githubOwner = it; prefs.edit().putString("owner", it).apply() },
                        label = { Text("Owner/Username") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = githubRepo,
                        onValueChange = { githubRepo = it; prefs.edit().putString("repo", it).apply() },
                        label = { Text("Repo Name") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = githubToken,
                        onValueChange = { githubToken = it; prefs.edit().putString("token", it).apply() },
                        label = { Text("GitHub Token (PAT)") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )

                    Spacer(modifier = Modifier.height(10.dp))
                    Text("Multiple Facebook Pages (1 per line)", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Text("Format: PageName|PageID|PageToken", fontSize = 11.sp, color = Color.Gray)
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = fbPagesRaw,
                        onValueChange = {
                            fbPagesRaw = it
                            prefs.edit().putString("fb_pages_raw", it).apply()
                        },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 3
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
            label = { Text("Custom Reel Title (Optional)") },
            placeholder = { Text("Movie Explained in Hindi") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Spacer(modifier = Modifier.height(6.dp))

        OutlinedTextField(
            value = customTags,
            onValueChange = {
                customTags = it
                prefs.edit().putString("custom_tags", it).apply()
            },
            label = { Text("Custom #Tags (Optional)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = videoUrl,
            onValueChange = { videoUrl = it },
            label = { Text("Paste Video Link (FB / YouTube)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Spacer(modifier = Modifier.height(8.dp))

        Text("Target Dubbing Language", modifier = Modifier.align(Alignment.Start), fontSize = 13.sp)
        Spacer(modifier = Modifier.height(4.dp))
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            listOf("hi" to "Hindi", "en" to "English (Re-voice)", "es" to "Spanish", "fr" to "French", "de" to "German").forEach { (code, name) ->
                FilterChip(
                    selected = selectedLanguage == code,
                    onClick = {
                        selectedLanguage = code
                        prefs.edit().putString("default_lang", code).apply()
                    },
                    label = { Text(name, fontSize = 11.sp) }
                )
            }
        }

        Spacer(modifier = Modifier.height(10.dp))

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
            modifier = Modifier.fillMaxWidth().height(48.dp),
            enabled = videoUrl.isNotBlank() && githubToken.isNotBlank()
        ) {
            Text("➕ Add to AutoDub Queue", fontSize = 15.sp)
        }

        Spacer(modifier = Modifier.height(10.dp))

        if (isUploadingToFb) {
            Card(
                modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Text(fbUploadStage, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                    Spacer(modifier = Modifier.height(6.dp))
                    LinearProgressIndicator(
                        progress = fbUploadPercent / 100f,
                        modifier = Modifier.fillMaxWidth()
                    )
                }
            }
        }

        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(8.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Text(DubberQueueManager.currentStatus, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                if (DubberQueueManager.firstLinePreview.isNotBlank()) {
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        "🗣️ 1st Translated Line: \"${DubberQueueManager.firstLinePreview}\"",
                        fontSize = 12.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = Color(0xFF00796B)
                    )
                }
                if (DubberQueueManager.detailedLogs.isNotBlank()) {
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        DubberQueueManager.detailedLogs,
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace
                    )
                }
            }
        }

        if (DubberQueueManager.historyList.isNotEmpty()) {
            Spacer(modifier = Modifier.height(14.dp))
            Text("🕒 Previous Download Links (Last 10)", fontWeight = FontWeight.Bold, fontSize = 15.sp, modifier = Modifier.align(Alignment.Start))
            Spacer(modifier = Modifier.height(8.dp))

            Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                DubberQueueManager.historyList.forEach { history ->
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
                    ) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(history.title, fontWeight = FontWeight.Bold, fontSize = 13.sp)
                                Text(history.timestamp, fontSize = 11.sp, color = Color.Gray)
                            }

                            Spacer(modifier = Modifier.height(4.dp))
                            Text(
                                history.downloadUrl,
                                fontSize = 11.sp,
                                color = MaterialTheme.colorScheme.primary,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.clickable {
                                    val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse(history.downloadUrl))
                                    context.startActivity(browserIntent)
                                }
                            )

                            Spacer(modifier = Modifier.height(8.dp))

                            var dropdownExpanded by remember { mutableStateOf(false) }
                            Box(modifier = Modifier.fillMaxWidth()) {
                                OutlinedButton(
                                    onClick = { dropdownExpanded = true },
                                    modifier = Modifier.fillMaxWidth().height(36.dp),
                                    contentPadding = PaddingValues(horizontal = 8.dp)
                                ) {
                                    Text(
                                        "Target: ${selectedPageAccount?.name ?: "Select Page"} ▼",
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

                            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                OutlinedButton(
                                    onClick = {
                                        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                                        clipboard.setPrimaryClip(ClipData.newPlainText("Dubbed Link", history.downloadUrl))
                                        Toast.makeText(context, "📋 Link copied!", Toast.LENGTH_SHORT).show()
                                    },
                                    modifier = Modifier.weight(1f).height(36.dp),
                                    contentPadding = PaddingValues(horizontal = 4.dp)
                                ) {
                                    Text("📋 Copy", fontSize = 11.sp)
                                }

                                Button(
                                    onClick = {
                                        val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse(history.downloadUrl))
                                        context.startActivity(browserIntent)
                                    },
                                    modifier = Modifier.weight(1f).height(36.dp),
                                    contentPadding = PaddingValues(horizontal = 4.dp)
                                ) {
                                    Text("⬇️ Open", fontSize = 11.sp)
                                }

                                Button(
                                    onClick = {
                                        val targetPage = selectedPageAccount
                                        if (targetPage == null) {
                                            Toast.makeText(context, "Configure at least one FB Page in Config!", Toast.LENGTH_SHORT).show()
                                            showSettings = true
                                        } else {
                                            isUploadingToFb = true
                                            val finalTitle = customTitle.ifBlank { "Movie Explained" }
                                            val finalTags = customTags.ifBlank { "#fyp #moviejet #reels #hindidubbed #movieexplained" }
                                            val finalCaption = "$finalTitle\n.\n.\n$finalTags"

                                            coroutineScope.launch {
                                                uploadUrlDirectlyToFacebook(
                                                    context = context,
                                                    pageId = targetPage.id,
                                                    pageToken = targetPage.token,
                                                    videoUrl = history.downloadUrl,
                                                    srtUrl = history.srtUrl,
                                                    description = finalCaption,
                                                    onProgress = { stage, percent ->
                                                        fbUploadStage = stage
                                                        fbUploadPercent = percent
                                                    },
                                                    onSuccess = {
                                                        isUploadingToFb = false
                                                        history.isUploaded = true
                                                        DubberQueueManager.saveHistory(context)
                                                        Toast.makeText(context, "🎉 Published Reel with Subtitles!", Toast.LENGTH_LONG).show()
                                                    },
                                                    onError = { err ->
                                                        isUploadingToFb = false
                                                        Toast.makeText(context, "❌ FB Upload Error: $err", Toast.LENGTH_LONG).show()
                                                    }
                                                )
                                            }
                                        }
                                    },
                                    modifier = Modifier.weight(1.3f).height(36.dp),
                                    colors = ButtonDefaults.buttonColors(
                                        containerColor = if (history.isUploaded) Color(0xFFE65100) else Color(0xFF1877F2)
                                    ),
                                    contentPadding = PaddingValues(horizontal = 4.dp)
                                ) {
                                    Text(
                                        if (history.isUploaded) "🔄 Re-upload" else "🚀 Upload FB",
                                        fontSize = 11.sp,
                                        color = Color.White
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

suspend fun executeCloudDubbingPipeline(
    context: Context,
    owner: String,
    repo: String,
    token: String,
    videoUrl: String,
    targetLang: String,
    speed: Float,
    onStatusUpdate: (String, String, String) -> Unit,
    onComplete: (String, String) -> Unit,
    onError: (String) -> Unit
) = withContext(Dispatchers.IO) {
    val client = OkHttpClient()
    val authHeader = "Bearer $token"

    try {
        onStatusUpdate("⚡ Dispatching Pipeline...", "Triggering GitHub Actions workflow...", "")

        val dispatchUrl = "https://api.github.com/repos/$owner/$repo/actions/workflows/dub_video.yml/dispatches"
        val payload = JSONObject().apply {
            put("ref", "main")
            put("inputs", JSONObject().apply {
                put("video_url", videoUrl)
                put("target_language", targetLang)
                put("video_speed", speed.toString())
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
            onError("HTTP ${response.code}: Check Token/Repo permissions")
            return@withContext
        }

        onStatusUpdate("⏳ Queued on GitHub...", "Waiting for cloud runner...", "")
        delay(6000)

        var runId: Long? = null
        for (i in 1..12) {
            val runsUrl = "https://api.github.com/repos/$owner/$repo/actions/runs?event=workflow_dispatch&per_page=1"
            val runsReq = Request.Builder().url(runsUrl).addHeader("Authorization", authHeader).build()
            val runsRes = client.newCall(runsReq).execute()
            if (runsRes.isSuccessful) {
                val json = JSONObject(runsRes.body?.string() ?: "")
                val runsArray = json.optJSONArray("workflow_runs")
                if (runsArray != null && runsArray.length() > 0) {
                    runId = runsArray.getJSONObject(0).getLong("id")
                    break
                }
            }
            delay(3000)
        }

        if (runId == null) {
            onError("Workflow run could not be found.")
            return@withContext
        }

        var isDone = false
        var runConclusion = ""
        var extractedPreview = ""

        while (!isDone) {
            delay(3500)
            val jobUrl = "https://api.github.com/repos/$owner/$repo/actions/runs/$runId/jobs"
            val jobReq = Request.Builder().url(jobUrl).addHeader("Authorization", authHeader).build()
            val jobRes = client.newCall(jobReq).execute()

            if (jobRes.isSuccessful) {
                val jobJson = JSONObject(jobRes.body?.string() ?: "")
                val jobs = jobJson.optJSONArray("jobs")
                if (jobs != null && jobs.length() > 0) {
                    val job = jobs.getJSONObject(0)
                    val jobId = job.optLong("id")
                    val status = job.optString("status")
                    val conclusion = job.optString("conclusion")
                    val steps = job.optJSONArray("steps")

                    var activeStep = "Executing dubbing pipeline..."
                    if (steps != null) {
                        for (j in 0 until steps.length()) {
                            val step = steps.getJSONObject(j)
                            if (step.optString("status") == "in_progress") {
                                activeStep = step.optString("name")
                                break
                            }
                        }
                    }

                    if (extractedPreview.isBlank() && jobId != 0L) {
                        try {
                            val logReq = Request.Builder()
                                .url("https://api.github.com/repos/$owner/$repo/actions/jobs/$jobId/logs")
                                .addHeader("Authorization", authHeader)
                                .build()
                            val logRes = client.newCall(logReq).execute()
                            if (logRes.isSuccessful) {
                                val logText = logRes.body?.string() ?: ""
                                val m = Pattern.compile("TRANSLATION_PREVIEW:\\s*(.+)").matcher(logText)
                                if (m.find()) {
                                    extractedPreview = m.group(1)?.trim() ?: ""
                                }
                            }
                        } catch (_: Exception) {}
                    }

                    onStatusUpdate("⚙️ $activeStep", "Run ID: $runId", extractedPreview)

                    if (status == "completed") {
                        isDone = true
                        runConclusion = conclusion
                    }
                }
            }
        }

        if (runConclusion != "success") {
            onError("Workflow failed with conclusion: $runConclusion")
            return@withContext
        }

        onStatusUpdate("🔗 Fetching Download Links...", "Querying release assets...", extractedPreview)
        delay(2000)

        val releaseUrl = "https://api.github.com/repos/$owner/$repo/releases/latest"
        val relReq = Request.Builder().url(releaseUrl).addHeader("Authorization", authHeader).build()
        val relRes = client.newCall(relReq).execute()

        var videoDownloadUrl = ""
        var srtDownloadUrl = ""

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
                    title = "Dubbed Video (#$runId)",
                    downloadUrl = videoDownloadUrl,
                    srtUrl = srtDownloadUrl,
                    timestamp = timeStr,
                    isUploaded = false
                )
            )
            onComplete(videoDownloadUrl, srtDownloadUrl)
        }

    } catch (e: Exception) {
        withContext(Dispatchers.Main) {
            onError(e.localizedMessage ?: "Process failed")
        }
    }
}

suspend fun uploadUrlDirectlyToFacebook(
    context: Context,
    pageId: String,
    pageToken: String,
    videoUrl: String,
    srtUrl: String,
    description: String,
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
            val errorMsg = initJson.optJSONObject("error")?.optString("message") ?: "Init session failed"
            withContext(Dispatchers.Main) { onError(errorMsg) }
            return@withContext
        }

        onProgress("Buffering cloud MP4...", 5)
        val sourceReq = Request.Builder().url(videoUrl).build()
        val sourceRes = uploadClient.newCall(sourceReq).execute()

        sourceRes.body!!.byteStream().use { input ->
            FileOutputStream(tempVideoFile).use { output -> input.copyTo(output) }
        }

        val countingBody = ProgressRequestBody(
            file = tempVideoFile,
            contentType = "application/octet-stream".toMediaType()
        ) { percent, sent, total ->
            val mbSent = sent / (1024 * 1024)
            val mbTotal = total / (1024 * 1024)
            onProgress("Uploading video: $percent% ($mbSent MB / $mbTotal MB)", percent)
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
                onProgress("Attaching .srt captions...", 95)
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

        onProgress("Publishing Reel to Facebook...", 99)
        val publishUrl = "https://graph.facebook.com/v20.0/$pageId/video_reels"
        val pubPayload = JSONObject().apply {
            put("upload_phase", "finish")
            put("access_token", pageToken)
            put("video_id", videoId)
            put("video_state", "PUBLISHED")
            put("description", description)
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
            val errorMsg = pubJson.optJSONObject("error")?.optString("message") ?: "Publishing failed"
            withContext(Dispatchers.Main) { onError(errorMsg) }
        }

    } catch (e: Exception) {
        withContext(Dispatchers.Main) { onError(e.localizedMessage ?: "Network error during upload") }
    } finally {
        if (tempVideoFile.exists()) tempVideoFile.delete()
        if (tempSrtFile.exists()) tempSrtFile.delete()
    }
}

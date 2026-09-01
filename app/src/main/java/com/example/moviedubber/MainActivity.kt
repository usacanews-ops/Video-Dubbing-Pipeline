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
import androidx.compose.foundation.background
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
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.regex.Pattern

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
    val timestamp: String
)

object DubberQueueManager {
    val queue = ConcurrentLinkedQueue<QueueItem>()
    var isProcessing by mutableStateOf(false)
    var currentStatus by mutableStateOf("Ready to dub")
    var detailedLogs by mutableStateOf("")
    var queueSize by mutableStateOf(0)
    var latestDownloadLink by mutableStateOf("")
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
                        timestamp = obj.getString("timestamp")
                    )
                )
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    fun addHistoryItem(context: Context, item: HistoryItem) {
        historyList.add(0, item)
        while (historyList.size > 10) {
            historyList.removeAt(historyList.size - 1)
        }
        val arr = JSONArray()
        for (h in historyList) {
            val obj = JSONObject().apply {
                put("id", h.id)
                put("title", h.title)
                put("downloadUrl", h.downloadUrl)
                put("srtUrl", h.srtUrl)
                put("timestamp", h.timestamp)
            }
            arr.put(obj)
        }
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        prefs.edit().putString("history_json", arr.toString()).apply()
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
                onStatusUpdate = { status, log ->
                    currentStatus = status
                    detailedLogs = log
                },
                onComplete = { dlUrl, srtUrl ->
                    latestDownloadLink = dlUrl
                    isProcessing = false
                    currentStatus = "🎉 Link Ready! Tap below to Copy or Upload."
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
            } else {
                Toast.makeText(this, "No valid video URL detected", Toast.LENGTH_SHORT).show()
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
    var fbPageId by remember { mutableStateOf(prefs.getString("fb_page_id", "") ?: "") }
    var fbPageToken by remember { mutableStateOf(prefs.getString("fb_page_token", "") ?: "") }

    var videoUrl by remember { mutableStateOf("") }
    var selectedLanguage by remember { mutableStateOf("hi") }
    var videoSpeed by remember { mutableStateOf(1.0f) }
    var showSettings by remember { mutableStateOf(githubToken.isBlank()) }
    var isUploadingToFb by remember { mutableStateOf(false) }

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
                        label = { Text("GitHub Personal Access Token (PAT)") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )

                    Spacer(modifier = Modifier.height(10.dp))
                    Text("Facebook Page Reels (Direct Upload)", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = fbPageId,
                        onValueChange = { fbPageId = it; prefs.edit().putString("fb_page_id", it).apply() },
                        label = { Text("Facebook Page ID") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = fbPageToken,
                        onValueChange = { fbPageToken = it; prefs.edit().putString("fb_page_token", it).apply() },
                        label = { Text("Page Access Token") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                }
            }
        }

        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = videoUrl,
            onValueChange = { videoUrl = it },
            label = { Text("Paste Video Link (or Share from FB/YT)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Spacer(modifier = Modifier.height(8.dp))

        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            listOf("hi" to "Hindi", "es" to "Spanish", "fr" to "French", "de" to "German").forEach { (code, name) ->
                FilterChip(
                    selected = selectedLanguage == code,
                    onClick = {
                        selectedLanguage = code
                        prefs.edit().putString("default_lang", code).apply()
                    },
                    label = { Text(name, fontSize = 12.sp) }
                )
            }
        }

        Spacer(modifier = Modifier.height(12.dp))

        Button(
            onClick = {
                if (videoUrl.isNotBlank()) {
                    DubberQueueManager.enqueue(
                        QueueItem(videoUrl = videoUrl.trim(), targetLang = selectedLanguage, speed = videoSpeed),
                        context
                    )
                    videoUrl = ""
                }
            },
            modifier = Modifier.fillMaxWidth().height(48.dp),
            enabled = videoUrl.isNotBlank() && githubToken.isNotBlank()
        ) {
            Text("➕ Add to Queue", fontSize = 15.sp)
        }

        Spacer(modifier = Modifier.height(12.dp))

        if (DubberQueueManager.isProcessing || isUploadingToFb) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            Spacer(modifier = Modifier.height(8.dp))
        }

        // Live Status
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(8.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Text(DubberQueueManager.currentStatus, fontWeight = FontWeight.Bold, fontSize = 14.sp)
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

        // Previous 10 Download Links with Direct Upload to FB & Copy Buttons
        if (DubberQueueManager.historyList.isNotEmpty()) {
            Spacer(modifier = Modifier.height(16.dp))
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
                                Text(history.title, fontWeight = FontWeight.Bold, fontSize = 14.sp)
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

                            Spacer(modifier = Modifier.height(10.dp))

                            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                // 1. Copy Link Button
                                OutlinedButton(
                                    onClick = {
                                        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                                        clipboard.setPrimaryClip(ClipData.newPlainText("Dubbed Link", history.downloadUrl))
                                        Toast.makeText(context, "📋 Link copied to clipboard!", Toast.LENGTH_SHORT).show()
                                    },
                                    modifier = Modifier.weight(1f).height(36.dp),
                                    contentPadding = PaddingValues(horizontal = 6.dp)
                                ) {
                                    Text("📋 Copy Link", fontSize = 11.sp)
                                }

                                // 2. Open / Download Link Button
                                Button(
                                    onClick = {
                                        val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse(history.downloadUrl))
                                        context.startActivity(browserIntent)
                                    },
                                    modifier = Modifier.weight(1f).height(36.dp),
                                    contentPadding = PaddingValues(horizontal = 6.dp)
                                ) {
                                    Text("⬇️ Open Link", fontSize = 11.sp)
                                }

                                // 3. Upload Directly to Facebook Button
                                Button(
                                    onClick = {
                                        if (fbPageId.isBlank() || fbPageToken.isBlank()) {
                                            Toast.makeText(context, "Configure FB Page ID and Token first!", Toast.LENGTH_SHORT).show()
                                            showSettings = true
                                        } else {
                                            isUploadingToFb = true
                                            coroutineScope.launch {
                                                uploadUrlDirectlyToFacebook(
                                                    pageId = fbPageId.trim(),
                                                    pageToken = fbPageToken.trim(),
                                                    videoUrl = history.downloadUrl,
                                                    onSuccess = {
                                                        isUploadingToFb = false
                                                        Toast.makeText(context, "🎉 Published directly to FB Reel!", Toast.LENGTH_LONG).show()
                                                    },
                                                    onError = { err ->
                                                        isUploadingToFb = false
                                                        Toast.makeText(context, "❌ FB Upload Error: $err", Toast.LENGTH_LONG).show()
                                                    }
                                                )
                                            }
                                        }
                                    },
                                    modifier = Modifier.weight(1.2f).height(36.dp),
                                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF1877F2)),
                                    contentPadding = PaddingValues(horizontal = 6.dp)
                                ) {
                                    Text("🚀 Upload FB", fontSize = 11.sp, color = Color.White)
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
// 🚀 Cloud Pipeline Orchestrator (Produces Direct CDN Download Links)
// =========================================================================
suspend fun executeCloudDubbingPipeline(
    context: Context,
    owner: String,
    repo: String,
    token: String,
    videoUrl: String,
    targetLang: String,
    speed: Float,
    onStatusUpdate: (String, String) -> Unit,
    onComplete: (String, String) -> Unit,
    onError: (String) -> Unit
) = withContext(Dispatchers.IO) {
    val client = OkHttpClient()
    val authHeader = "Bearer $token"

    try {
        onStatusUpdate("⚡ Dispatching Pipeline...", "Triggering GitHub Actions workflow...")

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

        onStatusUpdate("⏳ Queued on GitHub...", "Waiting for cloud runner...")
        delay(6000)

        // Poll for Workflow Run ID
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

        // Poll Steps
        var isDone = false
        var runConclusion = ""
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

                    onStatusUpdate("⚙️ $activeStep", "Run ID: $runId")

                    if (status == "completed") {
                        isDone = true
                        runConclusion = conclusion
                    }
                }
            }
        }

        if (runConclusion != "success") {
            onError("Workflow failed with: $runConclusion")
            return@withContext
        }

        // Retrieve Release Assets Direct CDN URLs
        onStatusUpdate("🔗 Generating Direct Links...", "Retrieving public release URLs...")
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
            // Fallback to GitHub run link if release asset isn't parsed
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
                    timestamp = timeStr
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

// =========================================================================
// 🎬 Direct Cloud-to-Facebook Reel Publisher (No Local Phone Download)
// =========================================================================
suspend fun uploadUrlDirectlyToFacebook(
    pageId: String,
    pageToken: String,
    videoUrl: String,
    onSuccess: () -> Unit,
    onError: (String) -> Unit
) = withContext(Dispatchers.IO) {
    val client = OkHttpClient()

    try {
        // Step 1: Initialize Reel Session on Facebook
        val initUrl = "https://graph.facebook.com/v20.0/$pageId/video_reels"
        val initPayload = JSONObject().apply {
            put("upload_phase", "start")
            put("access_token", pageToken)
        }
        val initReq = Request.Builder()
            .url(initUrl)
            .post(initPayload.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val initRes = client.newCall(initReq).execute()
        val initBody = initRes.body?.string() ?: ""
        val initJson = JSONObject(initBody)

        val videoId = initJson.optString("video_id")
        val uploadUrl = initJson.optString("upload_url")

        if (videoId.isBlank() || uploadUrl.isBlank()) {
            withContext(Dispatchers.Main) { onError(initJson.optJSONObject("error")?.optString("message") ?: "Failed to start upload session") }
            return@withContext
        }

        // Step 2: Stream directly from Cloud CDN to Facebook Ruploader
        val uploadReq = Request.Builder()
            .url(uploadUrl)
            .addHeader("Authorization", "OAuth $pageToken")
            .addHeader("file_url", videoUrl)
            .post(ByteArray(0).toRequestBody(null))
            .build()
        client.newCall(uploadReq).execute().close()

        // Step 3: Finish and Publish Reel
        val publishUrl = "https://graph.facebook.com/v20.0/$pageId/video_reels"
        val pubPayload = JSONObject().apply {
            put("upload_phase", "finish")
            put("access_token", pageToken)
            put("video_id", videoId)
            put("video_state", "PUBLISHED")
            put("description", "Hindi Explainer Video #reels #hindidubbed #movieexplained")
        }

        val pubReq = Request.Builder()
            .url(publishUrl)
            .post(pubPayload.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val pubRes = client.newCall(pubReq).execute()
        val pubBody = pubRes.body?.string() ?: ""
        val pubJson = JSONObject(pubBody)

        if (pubJson.optBoolean("success", false) || pubJson.has("video_id")) {
            withContext(Dispatchers.Main) { onSuccess() }
        } else {
            withContext(Dispatchers.Main) { onError(pubJson.optJSONObject("error")?.optString("message") ?: "Publishing failed") }
        }

    } catch (e: Exception) {
        withContext(Dispatchers.Main) { onError(e.localizedMessage ?: "Upload failed") }
    }
}

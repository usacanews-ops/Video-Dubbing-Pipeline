package com.example.moviedubber

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.FileProvider
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.*
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.regex.Pattern
import java.util.zip.ZipInputStream

data class QueueItem(
    val id: Long = System.currentTimeMillis(),
    val videoUrl: String,
    val targetLang: String = "hi",
    val speed: Float = 1.0f
)

object DubberQueueManager {
    val queue = ConcurrentLinkedQueue<QueueItem>()
    var isProcessing by mutableStateOf(false)
    var currentStatus by mutableStateOf("Ready to dub")
    var detailedLogs by mutableStateOf("")
    var queueSize by mutableStateOf(0)
    var lastDownloadedVideo by mutableStateOf<File?>(null)

    private val coroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

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
        val fbPageId = prefs.getString("fb_page_id", "") ?: ""
        val fbPageToken = prefs.getString("fb_page_token", "") ?: ""

        coroutineScope.launch {
            executeCloudDubbingPipeline(
                context = context,
                owner = owner.trim(),
                repo = repo.trim(),
                token = token.trim(),
                fbPageId = fbPageId.trim(),
                fbPageToken = fbPageToken.trim(),
                videoUrl = nextItem.videoUrl.trim(),
                targetLang = nextItem.targetLang,
                speed = nextItem.speed,
                onStatusUpdate = { status, log ->
                    currentStatus = status
                    detailedLogs = log
                },
                onComplete = { file ->
                    lastDownloadedVideo = file
                    isProcessing = false
                    currentStatus = "🎉 Done! Reel Published & Saved."
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
                Toast.makeText(this, "📥 Added to AutoDub Queue (#${DubberQueueManager.queueSize})", Toast.LENGTH_SHORT).show()
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
    val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)

    var githubOwner by remember { mutableStateOf(prefs.getString("owner", "usacanews-ops") ?: "") }
    var githubRepo by remember { mutableStateOf(prefs.getString("repo", "Video-Dubbing-Pipeline") ?: "") }
    var githubToken by remember { mutableStateOf(prefs.getString("token", "") ?: "") }
    var fbPageId by remember { mutableStateOf(prefs.getString("fb_page_id", "") ?: "") }
    var fbPageToken by remember { mutableStateOf(prefs.getString("fb_page_token", "") ?: "") }

    var videoUrl by remember { mutableStateOf("") }
    var selectedLanguage by remember { mutableStateOf("hi") }
    var videoSpeed by remember { mutableStateOf(1.0f) }
    var showSettings by remember { mutableStateOf(githubToken.isBlank() || fbPageToken.isBlank()) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(18.dp)
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
                        "Queue: ${DubberQueueManager.queueSize} waiting | Active: ${if (DubberQueueManager.isProcessing) "1" else "0"}",
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
                    Text("GitHub Settings (AI Dubbing)", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = githubOwner,
                        onValueChange = { githubOwner = it; prefs.edit().putString("owner", it).apply() },
                        label = { Text("GitHub Owner/Username") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = githubRepo,
                        onValueChange = { githubRepo = it; prefs.edit().putString("repo", it).apply() },
                        label = { Text("Repository Name") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = githubToken,
                        onValueChange = { githubToken = it; prefs.edit().putString("token", it).apply() },
                        label = { Text("GitHub Personal Access Token") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )

                    Spacer(modifier = Modifier.height(14.dp))
                    Text("Facebook Auto-Publishing (Reels)", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = fbPageId,
                        onValueChange = { fbPageId = it; prefs.edit().putString("fb_page_id", it).apply() },
                        label = { Text("Facebook Page ID") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(6.dp))
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

        Spacer(modifier = Modifier.height(12.dp))

        OutlinedTextField(
            value = videoUrl,
            onValueChange = { videoUrl = it },
            label = { Text("Paste Video URL (or share from FB/YT)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Spacer(modifier = Modifier.height(12.dp))

        Text("Target Language", modifier = Modifier.align(Alignment.Start), fontSize = 14.sp)
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("hi" to "Hindi", "es" to "Spanish", "fr" to "French", "de" to "German").forEach { (code, name) ->
                FilterChip(
                    selected = selectedLanguage == code,
                    onClick = {
                        selectedLanguage = code
                        prefs.edit().putString("default_lang", code).apply()
                    },
                    label = { Text(name) }
                )
            }
        }

        Spacer(modifier = Modifier.height(12.dp))

        Text("Speed Factor: ${String.format("%.2fx", videoSpeed)}", modifier = Modifier.align(Alignment.Start), fontSize = 14.sp)
        Slider(
            value = videoSpeed,
            onValueChange = {
                videoSpeed = it
                prefs.edit().putFloat("default_speed", it).apply()
            },
            valueRange = 0.75f..1.5f,
            steps = 5,
            modifier = Modifier.fillMaxWidth()
        )

        Spacer(modifier = Modifier.height(16.dp))

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
            modifier = Modifier.fillMaxWidth().height(50.dp),
            enabled = videoUrl.isNotBlank() && githubToken.isNotBlank()
        ) {
            Text("➕ Add to Dubbing & Reel Queue", fontSize = 15.sp)
        }

        Spacer(modifier = Modifier.height(16.dp))

        if (DubberQueueManager.isProcessing) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            Spacer(modifier = Modifier.height(10.dp))
        }

        // Live status & logs
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(8.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Text(DubberQueueManager.currentStatus, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                if (DubberQueueManager.detailedLogs.isNotBlank()) {
                    Spacer(modifier = Modifier.height(6.dp))
                    Text(
                        DubberQueueManager.detailedLogs,
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace
                    )
                }
            }
        }

        // Play last processed video
        DubberQueueManager.lastDownloadedVideo?.let { videoFile ->
            Spacer(modifier = Modifier.height(16.dp))
            Button(
                onClick = {
                    val uri: Uri = FileProvider.getUriForFile(context, "${context.packageName}.provider", videoFile)
                    val intent = Intent(Intent.ACTION_VIEW).apply {
                        setDataAndType(uri, "video/mp4")
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    }
                    context.startActivity(Intent.createChooser(intent, "Play Dubbed Video"))
                },
                modifier = Modifier.fillMaxWidth().height(50.dp),
                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2E7D32))
            ) {
                Text("▶️ Play Last Dubbed Reel", fontSize = 15.sp, color = Color.White)
            }
        }
    }
}

// =========================================================================
// 🚀 Cloud Pipeline Orchestrator + Facebook Reels Publisher
// =========================================================================
suspend fun executeCloudDubbingPipeline(
    context: Context,
    owner: String,
    repo: String,
    token: String,
    fbPageId: String,
    fbPageToken: String,
    videoUrl: String,
    targetLang: String,
    speed: Float,
    onStatusUpdate: (String, String) -> Unit,
    onComplete: (File) -> Unit,
    onError: (String) -> Unit
) = withContext(Dispatchers.IO) {
    val client = OkHttpClient()
    val authHeader = "Bearer $token"

    try {
        onStatusUpdate("⚡ Triggering Cloud Dubbing...", "Dispatching GitHub Actions workflow...")

        // 1. Dispatch Workflow
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
            onError("Failed to dispatch: HTTP ${response.code}. Check GitHub token/repo permissions.")
            return@withContext
        }

        onStatusUpdate("⏳ Queued on GitHub...", "Waiting for cloud runner to start...")
        delay(6000)

        // 2. Poll for Workflow Run ID
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
            onError("Could not find triggered run on GitHub.")
            return@withContext
        }

        // 3. Monitor Job Progress
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

                    var activeStepName = "Setting up cloud environment..."
                    if (steps != null) {
                        for (j in 0 until steps.length()) {
                            val step = steps.getJSONObject(j)
                            if (step.optString("status") == "in_progress") {
                                activeStepName = step.optString("name")
                                break
                            }
                        }
                    }

                    onStatusUpdate("⚙️ $activeStepName", "Workflow Run #$runId")

                    if (status == "completed") {
                        isDone = true
                        runConclusion = conclusion
                    }
                }
            }
        }

        if (runConclusion != "success") {
            onError("Pipeline failed with conclusion: $runConclusion.")
            return@withContext
        }

        // 4. Download Video & Subtitle Artifacts
        onStatusUpdate("📥 Fetching Dubbed Files...", "Downloading artifacts from GitHub...")
        val artifactsUrl = "https://api.github.com/repos/$owner/$repo/actions/runs/$runId/artifacts"
        val artReq = Request.Builder().url(artifactsUrl).addHeader("Authorization", authHeader).build()
        val artRes = client.newCall(artReq).execute()

        var downloadUrl: String? = null
        if (artRes.isSuccessful) {
            val artJson = JSONObject(artRes.body?.string() ?: "")
            val arts = artJson.optJSONArray("artifacts")
            if (arts != null && arts.length() > 0) {
                downloadUrl = arts.getJSONObject(0).getString("archive_download_url")
            }
        }

        if (downloadUrl == null) {
            onError("No artifacts found in completed workflow run.")
            return@withContext
        }

        val dlReq = Request.Builder().url(downloadUrl).addHeader("Authorization", authHeader).build()
        val dlRes = client.newCall(dlReq).execute()

        val outputDir = File(context.getExternalFilesDir(null), "Movies").apply { mkdirs() }
        val finalMp4 = File(outputDir, "dubbed_${System.currentTimeMillis()}.mp4")
        val finalSrt = File(outputDir, "dubbed_${System.currentTimeMillis()}.srt")

        dlRes.body?.byteStream()?.use { inputStream ->
            ZipInputStream(inputStream).use { zipStream ->
                var entry = zipStream.nextEntry
                while (entry != null) {
                    if (entry.name.endsWith(".mp4") || entry.name == "final_output.mp4") {
                        FileOutputStream(finalMp4).use { output -> zipStream.copyTo(output) }
                    } else if (entry.name.endsWith(".srt") || entry.name == "subtitles.srt") {
                        FileOutputStream(finalSrt).use { output -> zipStream.copyTo(output) }
                    }
                    entry = zipStream.nextEntry
                }
            }
        }

        // 5. Auto-publish to Facebook Page Reels (if configured)
        if (fbPageId.isNotBlank() && fbPageToken.isNotBlank() && finalMp4.exists()) {
            onStatusUpdate("📲 Publishing to Facebook Reels...", "Uploading to Page ID: $fbPageId")
            publishFacebookReel(
                client = client,
                pageId = fbPageId,
                pageToken = fbPageToken,
                videoFile = finalMp4
            )
        }

        withContext(Dispatchers.Main) {
            onComplete(finalMp4)
        }

    } catch (e: Exception) {
        withContext(Dispatchers.Main) {
            onError(e.localizedMessage ?: "Unknown error occurred")
        }
    }
}

// =========================================================================
// 🎬 Facebook Graph API - Direct Reel Uploader
// =========================================================================
private fun publishFacebookReel(
    client: OkHttpClient,
    pageId: String,
    pageToken: String,
    videoFile: File
) {
    try {
        // Step 1: Initialize Reel Upload Session
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
        val initBody = initRes.body?.string() ?: return
        val initJson = JSONObject(initBody)
        val videoId = initJson.optString("video_id")
        val uploadUrl = initJson.optString("upload_url")

        if (videoId.isBlank() || uploadUrl.isBlank()) return

        // Step 2: Binary Video Upload
        val mediaType = "application/octet-stream".toMediaType()
        val fileBody = videoFile.asRequestBody(mediaType)

        val uploadReq = Request.Builder()
            .url(uploadUrl)
            .addHeader("Authorization", "OAuth $pageToken")
            .addHeader("offset", "0")
            .addHeader("file_size", videoFile.length().toString())
            .post(fileBody)
            .build()

        client.newCall(uploadReq).execute().close()

        // Step 3: Publish the Reel
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

        client.newCall(pubReq).execute().close()
    } catch (e: Exception) {
        e.printStackTrace()
    }
}

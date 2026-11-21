function tovideo --description "Interactive video converter (VA-API iGPU/dGPU, Vulkan, or CPU for HEVC, H.264/WhatsApp, AV1)"
    argparse 'h/help' -- $argv
    or return 1

    if set -q _flag_help
        echo "Usage: tovideo [file...]"
        echo "Launches an interactive guide to pick the input file(s), codec/format,"
        echo "hardware backend (Ryzen iGPU, 7900 XTX dGPU, Vulkan, CPU), resolution, and quality."
        return 0
    end

    # ── 1. Input File Selection ──────────────────────────────────────────────────
    set -l inputs $argv
    if test (count $inputs) -eq 0
        echo "======================================================================"
        echo " 1) Select Input Video File"
        echo "======================================================================"
        set -l candidates (find . -maxdepth 1 -type f \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.mov" -o -iname "*.avi" -o -iname "*.webm" \) | sort)
        if test (count $candidates) -gt 0
            echo "Videos found in current folder:"
            for i in (seq (count $candidates))
                set -l f (string replace -r '^\./' '' -- $candidates[$i])
                set -l sz (du -h "$f" 2>/dev/null | cut -f1)
                echo "  [$i] $f ($sz)"
            end
            echo ""
            read -P "Enter file number (1-"(count $candidates)"), 'a' for all, or type a file path: " file_choice
            if test "$file_choice" = "a" -o "$file_choice" = "A"
                for c in $candidates
                    set -a inputs (string replace -r '^\./' '' -- "$c")
                end
            else if string match -qr '^[0-9]+$' -- "$file_choice"
                and test "$file_choice" -ge 1 -a "$file_choice" -le (count $candidates)
                set inputs (string replace -r '^\./' '' -- $candidates[$file_choice])
            else if test -n "$file_choice"
                set inputs "$file_choice"
            end
        else
            read -P "Enter path to video file: " manual_path
            if test -n "$manual_path"
                set inputs "$manual_path"
            end
        end
    end

    set -l valid_inputs
    for f in $inputs
        if test -f "$f"
            set -a valid_inputs "$f"
        else
            echo "Warning: File not found, skipping: $f" >&2
        end
    end

    if test (count $valid_inputs) -eq 0
        echo "No valid input files selected. Aborting." >&2
        return 1
    end

    # ── 2. Format & Codec Selection ──────────────────────────────────────────────
    echo ""
    echo "======================================================================"
    echo " 2) Choose Output Format & Codec"
    echo "======================================================================"
    echo "  [1] HEVC / H.265 in .mkv  (Default — Best for local archive & PC playback)"
    echo "      • ~50% smaller than H.264 at the same visual quality."
    echo "      • .mkv container is resilient and supports all audio formats."
    echo "      • Note: WhatsApp will NOT play .mkv inline."
    echo ""
    echo "  [2] WhatsApp / Universal Share — H.264 (AVC) in .mp4"
    echo "      • Plays inline everywhere: WhatsApp, Telegram, Discord, iOS, Android, Web."
    echo "      • Uses 8-bit yuv420p, AAC 128k audio, and +faststart for instant streaming."
    echo ""
    echo "  [3] AV1 in .mkv  (Modern Ultra-Compression)"
    echo "      • Royalty-free next-gen codec (~20–30% smaller than HEVC)."
    echo "      • Supported by CPU (SVT-AV1) and 7900 XTX dGPU (RDNA3 VCN4)."
    echo "      • Note: Ryzen iGPU (RDNA2) only decodes AV1; it cannot hardware-encode AV1."
    echo ""
    read -P "Select format [1-3, Enter=1]: " fmt_choice
    test -z "$fmt_choice"; and set fmt_choice 1

    set -l codec "hevc"
    set -l ext "mkv"
    set -l tag "hevc"
    switch "$fmt_choice"
        case 2
            set codec "h264"
            set ext "mp4"
            set tag "whatsapp"
        case 3
            set codec "av1"
            set ext "mkv"
            set tag "av1"
        case '*'
            set codec "hevc"
            set ext "mkv"
            set tag "hevc"
    end

    # ── 3. Hardware Device & Encoding Technology ─────────────────────────────────
    echo ""
    echo "======================================================================"
    echo " 3) Choose Hardware Device & Encoding Backend"
    echo "======================================================================"
    if test "$codec" = "av1"
        echo "  [1] CPU Software — SVT-AV1 (libsvtav1)  [Default for AV1]"
        echo "      • Best compression efficiency and visual quality per megabyte."
        echo "      • Uses multi-core CPU; slower than GPU."
        echo ""
        echo "  [2] Radeon RX 7900 XTX dGPU — VA-API (/dev/dri/renderD128)"
        echo "      • Very fast hardware AV1 encoder (RDNA3 VCN4)."
        echo "      • Wakes up discrete GPU (higher board power draw)."
        echo ""
        echo "  [3] Vulkan Video GPU (av1_vulkan)"
        echo "      • Uses FFmpeg's Vulkan Video encode pipeline."
        echo ""
        read -P "Select backend [1-3, Enter=1]: " hw_choice
        test -z "$hw_choice"; and set hw_choice 1
        switch "$hw_choice"
            case 2
                set tech "vaapi_dgpu"
            case 3
                set tech "vulkan"
            case '*'
                set tech "cpu"
        end
    else
        echo "  [1] Ryzen iGPU — VA-API (/dev/dri/renderD129)  [Default — Recommended]"
        echo "      • Fast hardware encode on integrated Raphael GPU (~5–15W power)."
        echo "      • Leaves the 7900 XTX dGPU untouched and avoids display/VRAM contention."
        echo ""
        echo "  [2] Radeon RX 7900 XTX dGPU — VA-API (/dev/dri/renderD128)"
        echo "      • Fast hardware encode on discrete GPU (higher idle/load power)."
        echo ""
        echo "  [3] Vulkan Video GPU ("$codec"_vulkan)"
        echo "      • Cross-vendor Vulkan Video hardware encode queue instead of Mesa VA-API."
        echo ""
        echo "  [4] CPU Software (libx265 / libx264)"
        echo "      • Slowest, uses all CPU threads, but achieves the smallest file size"
        echo "        for a given visual clarity."
        echo ""
        read -P "Select backend [1-4, Enter=1]: " hw_choice
        test -z "$hw_choice"; and set hw_choice 1
        switch "$hw_choice"
            case 2
                set tech "vaapi_dgpu"
            case 3
                set tech "vulkan"
            case 4
                set tech "cpu"
            case '*'
                set tech "vaapi_igpu"
        end
    end

    # ── 4. Resolution & Framerate ────────────────────────────────────────────────
    echo ""
    echo "======================================================================"
    echo " 4) Choose Resolution & Framerate"
    echo "======================================================================"
    if test "$tag" = "whatsapp"
        echo "  [1] Max 1080p @ 30 fps  [Default — Best for WhatsApp / Chat apps]"
        echo "      • Cuts 60 fps frame count in half and downscales 4K to 1080p."
        echo "  [2] Max 720p @ 30 fps   [Extra compact for long clips]"
        echo "  [3] Max 1080p @ 60 fps  [Smooth motion, larger file]"
        echo "  [4] Keep original resolution & framerate"
        echo ""
        read -P "Select resolution/FPS [1-4, Enter=1]: " res_choice
        test -z "$res_choice"; and set res_choice 1
    else
        echo "  [1] Keep original resolution & framerate  [Default]"
        echo "  [2] Max 1080p @ 60 fps  (Great for shrinking 4K60 while keeping 60 fps)"
        echo "  [3] Max 1080p @ 30 fps  (Halves frame data for much smaller files)"
        echo "  [4] Max 720p @ 30 fps   (Smallest file size)"
        echo ""
        read -P "Select resolution/FPS [1-4, Enter=1]: " res_choice
        test -z "$res_choice"; and set res_choice 1
        # Map choices to common filter key
        switch "$res_choice"
            case 1
                set res_choice 4
            case 2
                set res_choice 3
            case 3
                set res_choice 1
            case 4
                set res_choice 2
        end
    end

    set -l pre_vf ""
    switch "$res_choice"
        case 1
            set pre_vf "fps=30,scale='min(1920,iw)':-2"
        case 2
            set pre_vf "fps=30,scale='min(1280,iw)':-2"
        case 3
            set pre_vf "fps=60,scale='min(1920,iw)':-2"
        case '*'
            set pre_vf ""
    end

    # ── 5. Rate Control & Quality / File Size ────────────────────────────────────
    echo ""
    echo "======================================================================"
    echo " 5) Choose Quality & File Size Target"
    echo "======================================================================"
    echo "  • CQP / CRF (Constant Quality): Keeps visual sharpness constant; complex"
    echo "    or noisy microscope scenes use more MB, static scenes use very little."
    echo "    (Lower number = higher quality & larger file; 24=High, 28=Balanced, 32=Compact)"
    echo "  • VBR (Variable Bitrate in Mbps): Predictable file size per minute"
    echo "    (2 Mbps ≈ 15 MB/min, 3 Mbps ≈ 22 MB/min, 5 Mbps ≈ 37 MB/min)."
    echo ""
    if test "$tag" = "whatsapp"
        echo "  [1] Balanced WhatsApp — VBR 3 Mbps (~22 MB/min)  [Default]"
        echo "  [2] Compact WhatsApp  — VBR 2 Mbps (~15 MB/min)"
        echo "  [3] High Detail       — VBR 5 Mbps (~37 MB/min)"
        echo "  [4] Constant Quality  — CQP / CRF 28"
        echo "  [5] Custom QP/CRF or Bitrate"
        echo ""
        read -P "Select quality [1-5, Enter=1]: " q_choice
        test -z "$q_choice"; and set q_choice 1
        switch "$q_choice"
            case 2
                set rc_type "vbr"
                set rc_val 2
            case 3
                set rc_type "vbr"
                set rc_val 5
            case 4
                set rc_type "cqp"
                set rc_val 28
            case 5
                read -P "Use [1] CQP/CRF (18-45) or [2] VBR Bitrate (Mbps)? [1/2]: " custom_mode
                if test "$custom_mode" = "2"
                    set rc_type "vbr"
                    read -P "Enter target bitrate in Mbps (e.g. 3): " rc_val
                    test -z "$rc_val"; and set rc_val 3
                else
                    set rc_type "cqp"
                    read -P "Enter QP/CRF (18=lossless-like, 28=balanced, 34=small): " rc_val
                    test -z "$rc_val"; and set rc_val 28
                end
            case '*'
                set rc_type "vbr"
                set rc_val 3
        end
    else
        echo "  [1] Balanced Quality — CQP / CRF 28  [Default]"
        echo "  [2] Compact / Small  — CQP / CRF 32  (Much smaller files, good clarity)"
        echo "  [3] High Detail      — CQP / CRF 24  (Crisp fine markings, larger files)"
        echo "  [4] Target Bitrate   — VBR 3 Mbps    (~22 MB/min predictable size)"
        echo "  [5] Custom QP/CRF or Bitrate"
        echo ""
        read -P "Select quality [1-5, Enter=1]: " q_choice
        test -z "$q_choice"; and set q_choice 1
        switch "$q_choice"
            case 2
                set rc_type "cqp"
                set rc_val 32
            case 3
                set rc_type "cqp"
                set rc_val 24
            case 4
                set rc_type "vbr"
                set rc_val 3
            case 5
                read -P "Use [1] CQP/CRF (18-45) or [2] VBR Bitrate (Mbps)? [1/2]: " custom_mode
                if test "$custom_mode" = "2"
                    set rc_type "vbr"
                    read -P "Enter target bitrate in Mbps (e.g. 3): " rc_val
                    test -z "$rc_val"; and set rc_val 3
                else
                    set rc_type "cqp"
                    read -P "Enter QP/CRF (18=lossless-like, 28=balanced, 34=small): " rc_val
                    test -z "$rc_val"; and set rc_val 28
                end
            case '*'
                set rc_type "cqp"
                set rc_val 28
        end
    end

    # ── 6. Execute Conversion ────────────────────────────────────────────────────
    for input in $valid_inputs
        set -l stem (path change-extension '' "$input")
        set -l output "$stem"_"$tag"."$ext"

        set -l hw_args
        set -l vf_chain
        set -l enc_args
        set -l audio_args
        set -l extra_out_args

        if test -n "$pre_vf"
            set -a vf_chain "$pre_vf"
        end

        if test "$ext" = "mp4"
            set audio_args -map 0:v:0 -map 0:a? -c:a aac -b:a 128k
            set extra_out_args -movflags +faststart
        else
            set audio_args -c:a copy
        end

        switch "$tech"
            case "vaapi_igpu" "vaapi_dgpu"
                set -l node "/dev/dri/renderD129"
                test "$tech" = "vaapi_dgpu"; and set node "/dev/dri/renderD128"
                set -l enc_name "$codec"_vaapi

                if test -z "$pre_vf"
                    # Zero-copy GPU decode when possible, auto-upload when software decoded
                    set hw_args -init_hw_device "vaapi=va:$node" -filter_hw_device va \
                        -hwaccel vaapi -hwaccel_device va -hwaccel_output_format vaapi
                    set -a vf_chain "format=nv12|vaapi,hwupload"
                else
                    # CPU scale/fps filter followed by NV12 upload to VA-API
                    set hw_args -vaapi_device "$node"
                    set -a vf_chain "format=nv12,hwupload"
                end

                if test "$rc_type" = "vbr"
                    set -l max_b (math "$rc_val + 2")
                    set enc_args -c:v "$enc_name" -rc_mode VBR -b:v "$rc_val"M -maxrate "$max_b"M
                else
                    set enc_args -c:v "$enc_name" -rc_mode CQP -qp "$rc_val"
                end

            case "vulkan"
                set -l enc_name "$codec"_vulkan
                set hw_args -init_hw_device vulkan
                set -a vf_chain "format=nv12,hwupload"
                if test "$rc_type" = "vbr"
                    set enc_args -c:v "$enc_name" -b:v "$rc_val"M
                else
                    set enc_args -c:v "$enc_name" -qp "$rc_val"
                end

            case "cpu"
                set -a vf_chain "format=yuv420p"
                if test "$codec" = "av1"
                    if test "$rc_type" = "vbr"
                        set enc_args -c:v libsvtav1 -preset 6 -rc 1 -b:v "$rc_val"M
                    else
                        set enc_args -c:v libsvtav1 -preset 6 -crf "$rc_val" -svtav1-params tune=0
                    end
                else if test "$codec" = "h264"
                    if test "$rc_type" = "vbr"
                        set -l max_b (math "$rc_val + 1")
                        set enc_args -c:v libx264 -preset medium -profile:v high -level 4.1 -b:v "$rc_val"M -maxrate "$max_b"M -bufsize "$max_b"M
                    else
                        set enc_args -c:v libx264 -preset medium -profile:v high -level 4.1 -crf "$rc_val"
                    end
                else
                    if test "$rc_type" = "vbr"
                        set enc_args -c:v libx265 -preset medium -b:v "$rc_val"M
                    else
                        set enc_args -c:v libx265 -preset medium -crf "$rc_val"
                    end
                end
        end

        set -l vf_str (string join "," $vf_chain)

        echo ""
        echo "----------------------------------------------------------------------"
        echo "Converting: $input -> $output"
        echo "Settings:   codec=$codec | backend=$tech | rate=$rc_type($rc_val) | vf=$vf_str"
        echo "----------------------------------------------------------------------"

        ffmpeg -hide_banner -loglevel warning -stats \
            $hw_args \
            -i "$input" \
            -vf "$vf_str" \
            $enc_args \
            $audio_args \
            $extra_out_args \
            "$output"

        if test $status -eq 0
            set -l before (du -h "$input" | cut -f1)
            set -l after (du -h "$output" | cut -f1)
            echo "Done: $before -> $after ($output)"
        else
            echo "Error: FFmpeg exited with a non-zero status for $input" >&2
        end
    end
end

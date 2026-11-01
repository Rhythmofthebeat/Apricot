# Avants AI: Hiring Platform

Avants AI is a hiring platform where employers post jobs and get AI resume scoring and AI voice interviews of candidates before reviewing them. Candidates apply, get matched to jobs, and interview with an AI.

**Live site:** https://avants.ai

## Where this started

Avants AI began as a free Community Edition that I designed and coded myself starting in July 2025: resume analysis, job matching, audio summaries, and PDF reports. That code and its full commit history are in [Rhythmofthebeat/Resume](https://github.com/Rhythmofthebeat/Resume). The Community Edition remains free.

## How this version was built

In June 2026 I began expanding Avants AI into this platform. I designed the features, wrote the interview evaluation guidelines, and tested every change on the live site. Most of the code in this repo was written by AI coding agents (Claude Code and OpenAI Codex) working from my instructions. CLAUDE.md and AGENTS.md hold the rules they follow, including the database safety rules I added after an agent wiped the production database in August 2026. The platform now also runs a daily off-site database backup.

## Features

**For employers:** job posting, AI resume scoring, AI voice interviews, applicant review pipeline, meeting scheduling, and analytics.

**For candidates:** job search and matching, AI interviews with coaching feedback, video interview scans, and resume tools.

## Built with

Python, Flask, Supabase (Postgres, auth, storage), the OpenAI API (GPT and Whisper), Google Cloud Text-to-Speech, Daily.co video, and Stripe, deployed on Render.

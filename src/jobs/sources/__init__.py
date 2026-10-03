"""Job sources. Each implements `fetch(SearchQuery) -> list[JobPosting]`.

| Board            | Module        | Mechanism                                   |
| ---------------- | ------------- | ------------------------------------------- |
| Reed             | job_boards    | Official Jobseeker API (free key)           |
| CV-Library       | job_boards    | Official Job Search API (partner key)       |
| Biotechnology Jobs | feeds       | Official JSON Feed (CC BY 4.0, hourly, latest 50) |
| Company sites    | companies     | Greenhouse / Lever / Ashby feeds, JSON-LD   |
| LinkedIn         | public_boards | Public (logged-out) job search, slow + capped |
| Totaljobs, jobs.ac.uk, NHS Jobs | public_boards | Public search pages / XML API |
| LinkedIn, Indeed | inbox         | Alert emails + user-saved postings          |
| Gmail alerts    | gmail_alerts  | Manual inbox sync; new jobs per selected CV |
"""

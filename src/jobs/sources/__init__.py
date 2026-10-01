"""Job sources. Each implements `fetch(SearchQuery) -> list[JobPosting]`.

| Board            | Module        | Mechanism                                   |
| ---------------- | ------------- | ------------------------------------------- |
| Reed             | job_boards    | Official Jobseeker API (free key)           |
| CV-Library       | job_boards    | Official Job Search API (partner key)       |
| Company sites    | companies     | Greenhouse / Lever / Ashby feeds, JSON-LD   |
| LinkedIn, Indeed | inbox         | Alert emails + user-saved postings          |
"""

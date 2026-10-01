# whatsapp-vehicle-bot — project instructions

## Terminology (use these names, never a bare "admin")

| Name | Who | In code |
|---|---|---|
| **FleetManagers** | Customers who message the bot to lock / unlock / panic their vehicles | `data/fleet_managers.csv`, `get_fleet_manager()`, `fm` |
| **ControlRoomAdmins** | The 12 shift staff (3 shifts × 4) who approve or reject FleetManager requests | `data/control_room_admins.csv` (`cra_id` CRA001…), `get_control_room_admin()`, `get_control_room_admins_on_duty()`, `cra` |
| **EngineeringTeam_<ROLE>** | People building this project. They sign in to the `/elockadmins` roster portal and are the fallback approvers when no ControlRoomAdmin is on shift | `ENGINEERING_TEAM_<ROLE>=<phone>` in `.env`, `ENGINEERING_TEAM`, `is_engineering_team()`, `engineering_team_role()` |

EngineeringTeam roles:

- `EngineeringTeam_PM`: Anshu (product manager), 918882374849
- `EngineeringTeam_CTO`: Head of Hardware Engineering / team lead, 919115999559
- `EngineeringTeam_CEO`: Anuj, 919311516839 (number still to be confirmed)
- `EngineeringTeam_HOS`: Priya, Head of Software (number not yet configured)

"Approver" is a role in the flow, not a group: whoever was sent a given request. That is the on-duty ControlRoomAdmins, or the EngineeringTeam when the request escalated.

Customer-facing WhatsApp text to FleetManagers stays in plain language ("fleet manager", no approver names or internal monikers).

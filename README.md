# Camera Configuration System with User Management

This application provides a comprehensive solution for managing camera configurations, viewing layouts, and user access control.

## Features

- **User Management**: Role-based access control with three user levels (super admin, admin, user)
- **Site Management**: Configure and manage sites with NVR credentials
- **Camera Management**: Configure camera RTSP streams and preview cameras
- **PC Management**: Configure streaming PCs and monitor layouts
- **Screen Layout**: Design viewing layouts for cameras

## User Roles

The system features three user roles with different permission levels:

1. **Super Admin**:
   - Full access to all features
   - Can create, edit, or delete any user
   - Can manage all configurations

2. **Admin**:
   - Full access to all features except user management
   - Can create new users and delete regular users
   - Cannot delete other admins or super admins

3. **User**:
   - Read-only access to site, camera, and PC configurations
   - Can create and manage view layouts
   - Can assign cameras to view layouts

## Installation & Setup

1. Clone the repository
2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Set up environment variables (optional):
   - `DB_PATH`: Path to SQLite database (default: `config.db`)
   - `STREAM_APP_WS_URL`: WebSocket URL for the streaming application (default: `ws://localhost:8765`)
   - `ENABLE_EMAIL`: Enable email invitations (default: `false`)
   - `SMTP_SERVER`: SMTP server address
   - `SMTP_PORT`: SMTP server port (default: `587`)
   - `SMTP_USERNAME`: SMTP username
   - `SMTP_PASSWORD`: SMTP password
   - `EMAIL_FROM`: Sender email address
   - `APP_URL`: Application URL for invitation links (default: `http://localhost:8501`)

4. Run the application:
   ```
   streamlit run main.py
   ```

## Email Invitations

The system supports sending email invitations to new users. To enable this feature:

1. Set `ENABLE_EMAIL` environment variable to `true`
2. Configure the SMTP settings with the environment variables
3. Set `APP_URL` to your public-facing application URL (for invitation links)

If email functionality is disabled, invitation tokens will be displayed on the screen for manual sharing.

## Default Login

On first run, the system creates a default super admin account:
- Username: `admin`
- Email: `admin@example.com`
- Password: `admin123`

**Important**: Change the default password immediately after first login!

## User Invitation Flow

1. Admin/Super Admin creates a new user
2. System generates an invitation token
3. If email is enabled, an invitation email is sent to the user
4. User clicks the invitation link and sets their password
5. User is automatically logged in after setting the password

## Security Notes

- Passwords are stored using secure PBKDF2 hashing with salt
- Authentication tokens expire after specified periods
- Session management ensures proper access control

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details. 
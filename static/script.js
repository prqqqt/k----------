$(document).ready(function(){
    $('#67').on('submit', function(e){
        e.preventDefault();

        var $error = $('#form-error');
        $error.hide().text('');

        var password = $('#password').val();
        var confirm = $('#confirm_password').val();

        if ($('#fullname').val().trim() === '' || password === '' || password !== confirm) {
            $error.text('Заполните имя и проверьте, что пароли совпадают').show();
            return;
        }

        $.ajax({
            url: '/user_register',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({
                name: $('#fullname').val().trim(),
                password: password,
                email: $('#email').val().trim()
            })
        }).done(function(resp){
            // после успешной регистрации открываем профиль
            window.location.href = (resp && resp.redirect) || '/profile';
        }).fail(function(xhr){
            var msg = (xhr.responseJSON && xhr.responseJSON.error) || 'Не удалось зарегистрироваться';
            $error.text(msg).show();
        });
    });
});
